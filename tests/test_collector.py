import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.collector import MARKETS, collect_once, format_summary, game_state, normalize_prop
from app.config import load_settings
from app.signal import american_to_prob, devig, line_move, signal
from app.store import Store

NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
G1, G2 = "g-1", "g-2"


def row(pid="p1", game=G1, name="Player One", line=24.5, over=-115, under=-105, open_line=23.5, **kw):
    r = {"player_id": pid, "game_id": game, "name": name, "team": "OKC", "opponent": "DEN", "venue": "home", "line": line,
         "over_price": over, "under_price": under, "open_line": open_line, "snapshot": "open", "actual": None, "result": None}
    r.update(kw)
    return r


class FakeHW:
    def __init__(self, boards=None, games=None, fail=()):
        self.boards = boards if boards is not None else {"pts": [row()]}
        self.games = games if games is not None else [
            {"id": G1, "home": "OKC", "away": "DEN", "status": "scheduled", "tipoff_utc": "2026-11-14T00:00:00Z"},
            {"id": G2, "home": {"abbreviation": "LAL"}, "away": "BOS", "status": "final", "tipoff_utc": "2026-11-13T01:00:00Z"}]
        self.fail = set(fail)
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/games":
            if "games" in self.fail:
                raise RuntimeError("boom")
            return {"data": self.games}
        assert path == "/props"
        if params["market"] in self.fail:
            raise RuntimeError("boom")
        return {"data": self.boards.get(params["market"], [])}


def run(hw, store, now=NOW):
    return asyncio.run(collect_once(hw, store, "2026-11-13", now))


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_devig_and_signal_strength_and_side():
    assert american_to_prob(-110) == pytest.approx(110 / 210) and american_to_prob(+100) == 0.5
    assert american_to_prob(0) is None and american_to_prob("x") is None
    assert devig(-110, -110) == pytest.approx(0.5)
    s = signal(-130, +110)                       # the market likes the over
    assert s["side"] == "over" and 0 < s["strength"] < 15 and s["p_over"] > 0.5
    assert signal(+110, -130)["side"] == "under"
    assert signal(None, -110) == {"p_over": None, "side": None, "strength": None}
    assert line_move(24.5, 23.5) == 1.0 and line_move(None, 23.5) is None


def test_game_state_follows_status_and_the_three_hour_window():
    tip = "2026-11-14T00:00:00Z"
    at = lambda **kw: datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc) + timedelta(**kw)
    assert game_state("scheduled", tip, at(hours=-4)) == "upcoming"
    assert game_state("scheduled", tip, at(hours=-3)) == "active"
    assert game_state("scheduled", tip, at(minutes=-1)) == "active"
    assert game_state("scheduled", tip, at(minutes=1)) == "tipped"
    assert game_state("live", tip, at(hours=-5)) == "tipped"
    assert game_state("Final", tip, at(hours=-5)) == "final"
    assert game_state(None, "garbage", at()) == "upcoming"


def test_first_collection_stores_games_props_and_the_run(store):
    s = run(FakeHW({"pts": [row(), row("p2", name="Two", over=+105, under=-125)], "ast": [row("p1", line=6.5, over=-110, under=-110)]}), store)
    assert s["ok"] and s["rows"] == 3 and s["inserted"] == 3 and s["games"] == 2
    assert s["game_states"] == {"upcoming": 1, "active": 0, "tipped": 0, "final": 1}
    props = store.props("2026-11-13")
    assert len(props) == 3 and props[0]["strength"] >= props[-1]["strength"]       # sorted by strength
    assert {g["home"] for g in store.games_for("2026-11-13")} == {"OKC", "LAL"}   # dict teams are handled
    assert store.last_run()["ok"] == 1 and "per market" in format_summary(s, store).replace("per market:", "per market")


def test_unchanged_prices_add_no_history_but_a_move_does(store):
    run(FakeHW(), store)
    assert run(FakeHW(), store)["changed"] == 0
    assert len(store.history("p1", G1, "pts")) == 1
    s = run(FakeHW({"pts": [row(line=25.5, over=-110, under=-110)]}), store, NOW + timedelta(hours=2))
    assert s["changed"] == 1
    h = store.history("p1", G1, "pts")
    assert [x["line"] for x in h] == [24.5, 25.5]
    cur = store.props("2026-11-13", "pts")[0]
    assert cur["line_move"] == 2.0 and cur["first_seen_at"] != cur["last_seen_at"]


def test_a_prop_that_disappears_is_inactive_but_an_empty_board_wipes_nothing(store):
    run(FakeHW({"pts": [row(), row("p2", name="Two")]}), store)
    s = run(FakeHW({"pts": [row()]}), store, NOW + timedelta(hours=1))
    assert s["deactivated"] == 1
    assert [p["player_id"] for p in store.props("2026-11-13")] == ["p1"]
    assert len(store.props("2026-11-13", active_only=False)) == 2
    s = run(FakeHW({}), store, NOW + timedelta(hours=2))
    assert s["deactivated"] == 0 and any("empty" in w for w in s["warnings"])
    assert [p["player_id"] for p in store.props("2026-11-13")] == ["p1"]


def test_a_failed_market_is_recorded_and_does_not_deactivate_its_props(store):
    run(FakeHW({"pts": [row()], "ast": [row(line=6.5)]}), store)
    s = run(FakeHW({"pts": [row()]}, fail={"ast"}), store, NOW + timedelta(hours=1))
    assert s["ok"] and s["failed"] == ["ast"] and "ast: RuntimeError" in s["errors"]
    assert len(store.props("2026-11-13", "ast")) == 1


def test_everything_failing_is_not_ok_and_games_failure_is_not_fatal(store):
    assert run(FakeHW(fail=set(MARKETS) | {"games"}), store)["ok"] is False
    s = run(FakeHW(fail={"games"}), store)
    assert s["ok"] and s["games"] == 0 and any(e.startswith("games") for e in s["errors"])
    assert len(store.props("2026-11-13")) == 1


def test_rows_without_ids_are_skipped_and_missing_prices_give_no_signal(store):
    s = run(FakeHW({"pts": [row(player_id=None), row("p3", over=None), row("p4", game=None)]}), store)
    assert s["rows"] == 1 and s["skipped_no_ids"] == 2
    p = store.props("2026-11-13")[0]
    assert p["strength"] is None and p["side"] is None
    assert normalize_prop({"player_id": "x", "game_id": "y"}, "pts", "d")["line_move"] is None


def test_results_are_picked_up_when_the_game_finishes(store):
    run(FakeHW(), store)
    run(FakeHW({"pts": [row(actual=27, result="over")]}), store, NOW + timedelta(hours=6))
    p = store.props("2026-11-13")[0]
    assert p["actual"] == 27 and p["result"] == "over"


def test_every_market_is_requested_with_an_explicit_date_and_db_path_setting(tmp_path):
    hw = FakeHW()
    run(hw, Store(":memory:"))
    asked = sorted(p["market"] for path, p in hw.calls if path == "/props")
    assert asked == sorted(MARKETS) and all(p["date"] == "2026-11-13" for _x, p in hw.calls)
    assert str(load_settings({"DB_PATH": "x/y.db"}).db_path).endswith("x/y.db")
    assert str(load_settings({"DB_PATH": str(tmp_path / "a.db")}).db_path) == str(tmp_path / "a.db")
    assert str(load_settings({}).db_path).endswith("data/props.db")


def test_percentiles_rank_within_each_market_not_across_markets():
    from app.signal import add_percentiles, is_void
    rows = [{"market": "fg3m", "strength": 40.0, "name": "a"}, {"market": "fg3m", "strength": 30.0, "name": "b"},
            {"market": "pts", "strength": 3.0, "name": "c"}, {"market": "pts", "strength": 1.0, "name": "d"},
            {"market": "pts", "strength": 3.0, "name": "e"}, {"market": "pts", "strength": None, "name": "f"}]
    out = {r["name"]: r["pct"] for r in add_percentiles(rows)}
    assert out["a"] == 75.0 and out["b"] == 25.0          # two rows: mid-ranks 0.75 and 0.25
    assert out["c"] == out["e"] == 66.7 and out["d"] == 16.7 and out["f"] is None
    assert is_void("dnp") and is_void("PUSH") and not is_void("over") and not is_void(None)


def test_game_slice_uses_the_percentile_of_the_whole_market(store):
    boards = {"pts": [row("p1", over=-300, under=+240), row("p2", over=-110, under=-110), row("p3", game=G2, over=-110, under=-110, name="Three")]}
    run(FakeHW(boards), store)
    whole = {p["player_id"]: p["pct"] for p in store.props("2026-11-13", "pts")}
    in_g1 = {p["player_id"]: p["pct"] for p in store.props("2026-11-13", "pts", game_id=G1)}
    assert in_g1 == {k: whole[k] for k in in_g1} and whole["p1"] > whole["p2"]
    allm = store.props("2026-11-13", game_id=G1)
    assert [p["player_id"] for p in allm] == ["p1", "p2"] and all(p["pct"] is not None for p in allm)
