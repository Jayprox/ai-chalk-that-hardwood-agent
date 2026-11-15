import asyncio
import plistlib
from datetime import datetime, timedelta, timezone

import pytest

from app.collector import collect_once
from app.config import load_settings
from app.dashboard import build_overview
from app.scheduler import Schedule, decide, launchd_plist, next_collection, tick
from app.store import Store
from tests.test_collector import FakeHW, row

S = Schedule()                                           # early 120, active 30, window 3 h, settle 60, retry 10
TIP = "2026-11-14T00:00:00Z"                             # window opens 21:00Z on the 13th


def at(h, m=0, day=13):
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


def game(status="scheduled", tip=TIP, gid="g"):
    return {"game_id": gid, "status": status, "tipoff_utc": tip}


def run(finished, ok=True):
    return {"started_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": int(ok)}


def test_no_games_and_no_run_collects_then_waits_the_early_interval():
    d = decide(at(15), "2026-11-13", [], None, S)
    assert d.due and "first" in d.reason
    d = decide(at(15), "2026-11-13", [], run(at(14, 30)), S)
    assert not d.due and d.next_due == at(16, 30)


def test_early_in_the_day_the_board_refreshes_every_two_hours():
    g = [game()]
    assert not decide(at(15), "d", g, run(at(13, 30)), S).due
    d = decide(at(15), "d", g, run(at(12, 50)), S)
    assert d.due and d.interval_min == 120


def test_a_collection_happens_right_when_the_pre_tip_window_opens():
    g = [game()]
    assert decide(at(21, 1), "d", g, run(at(20, 45)), S).due
    d = decide(at(20), "d", g, run(at(19, 30)), S)       # the 2-hour mark (21:30) is after the window opens: wait for 21:00 instead
    assert not d.due and d.next_due == at(21)


def test_inside_the_window_the_cadence_is_thirty_minutes():
    g = [game()]
    d = decide(at(22), "d", g, run(at(21, 40)), S)
    assert not d.due and d.next_due == at(22, 10) and d.interval_min == 30
    assert decide(at(22), "d", g, run(at(21, 25)), S).due


def test_after_tip_off_it_only_settles_hourly_and_stops_when_all_final():
    g = [game()]
    d = decide(at(0, 30, day=14), "d", g, run(at(0, 0, day=14)), S)
    assert not d.due and d.interval_min == 60 and d.next_due == at(1, 0, day=14)
    assert decide(at(1, 5, day=14), "d", g, run(at(0, 0, day=14)), S).due
    done = decide(at(5, day=14), "d", [game("final")], run(at(4, 50, day=14)), S)
    assert not done.due and done.next_due is None and "final" in done.reason


def test_a_game_that_never_finishes_is_abandoned_after_a_day():
    d = decide(at(12, day=15), "d", [game()], run(at(1, day=14)), S)
    assert not d.due and d.next_due is None


def test_failures_retry_sooner_and_nothing_collects_twice_within_five_minutes():
    g = [game()]
    assert decide(at(15), "d", g, run(at(14, 48), ok=False), S).due
    assert not decide(at(15), "d", g, run(at(14, 54), ok=False), S).due
    fast = Schedule(active_min=1)
    assert not decide(at(22), "d", g, run(at(21, 57)), fast).due and decide(at(22), "d", g, run(at(21, 54)), fast).due


def test_with_one_game_in_its_window_the_faster_cadence_applies_to_the_whole_date():
    g = [game(gid="a"), game(tip="2026-11-14T03:00:00Z", gid="b")]
    d = decide(at(22), "d", g, run(at(21, 20)), S)
    assert d.due and d.interval_min == 30 and d.phases == {"active": 1, "upcoming": 1}


def _seed(tmp_path):
    store = Store(tmp_path / "s.db")
    y = {"id": "gy", "home": "OKC", "away": "DEN", "status": "scheduled", "tipoff_utc": "2026-11-13T01:00:00Z"}
    asyncio.run(collect_once(FakeHW({"pts": [row()]}, games=[y]), store, "2026-11-12", at(23, day=12)))
    return store, dict(y, status="final")


def test_a_tick_collects_unsettled_yesterday_and_today_then_goes_quiet(tmp_path):
    store, final = _seed(tmp_path)
    hw = FakeHW({"pts": [row()]}, games=[final])
    out = asyncio.run(tick(hw, store, at(15), S))
    assert [r["date"] for r in out] == ["2026-11-12", "2026-11-13"]
    assert "first collection" in out[1]["reason"] and "interval" in out[0]["reason"]
    assert asyncio.run(tick(hw, store, at(15, 10), S)) == []                      # yesterday is final, today was just collected
    store.close()


def test_the_dashboard_shows_the_next_collection_and_flags_a_stopped_scheduler(tmp_path):
    store = Store(tmp_path / "d.db")
    asyncio.run(collect_once(FakeHW(), store, "2026-11-13", at(20)))               # games: g-1 (tip 00:00Z next day), g-2 final
    info = {"date": "2026-11-13", "requested": "2026-11-13", "fallback": False}
    o = build_overview(store, info, S, at(20, 10))
    assert o["next_run"] == "4:00 PM ET" and o["scheduler"]["stale"] is False
    cards = {g["game_id"]: g for g in o["games"]}
    assert cards["g-1"]["refresh"] == "Next about 4:00 PM ET" and cards["g-2"]["refresh"] == ""
    late = build_overview(store, info, S, at(23, 30))                              # window open since 21:00, nothing collected since 20:00
    assert late["scheduler"]["stale"] is True and late["scheduler"]["due_now"] is True
    assert next_collection(store, at(20, 10), S, "2026-11-13")["stale"] is False
    store.close()


def test_launchd_plist_is_valid_and_points_at_this_checkout():
    plist = plistlib.loads(launchd_plist("/venv/bin/python", "/repo", "/repo/logs/scheduler.log").encode())
    assert plist["ProgramArguments"] == ["/venv/bin/python", "-m", "app.scheduler", "--once"]
    assert plist["WorkingDirectory"] == "/repo" and plist["StartInterval"] == 300 and plist["RunAtLoad"] is True


def test_schedule_comes_from_config_with_sane_bounds():
    d = Schedule.from_settings(load_settings({}))
    assert (d.early_min, d.active_min, d.window_hours, d.settle_min, d.retry_min) == (120, 30, 3.0, 60, 10)
    s = Schedule.from_settings(load_settings({"COLLECT_EARLY_MIN": "5", "COLLECT_ACTIVE_MIN": "1", "COLLECT_WINDOW_HOURS": "24", "COLLECT_SETTLE_MIN": "0"}))
    assert (s.early_min, s.active_min, s.window_hours, s.settle_min) == (5, 1, 24.0, 1)    # settle is clamped up to 1
    with pytest.raises(ValueError):
        load_settings({"COLLECT_ACTIVE_MIN": "soon"})
