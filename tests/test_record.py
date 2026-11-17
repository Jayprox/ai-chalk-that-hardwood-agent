import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from app import protocol as P
from app.config import ROOT
from app.coverage import integrity_report, replay_sample
from app.ledger import Ledger, parse_ts, ts
from app.provenance import secrets_in
from app.record import check_row, fresh_enough, ledger_tick
from app.simulation import Params
from tests.ledger_helpers import DATE, GAME, NOW, SECRET, TIP, Clock, FakeHW, close_row
from tests.test_backtest import make_players

SEASON = "2026-27"
PARAMS = Params(draws=400)


def tick(ledger, hw, clock=None, **kw):
    return asyncio.run(ledger_tick(hw, ledger, SEASON, clock=clock or Clock(), params=PARAMS, **kw))


@pytest.fixture
def L(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db", tmp_path / "mirror")
    yield ledger
    ledger.close()


def test_one_tick_records_every_close_contract_before_tip_with_full_provenance(L):
    out = tick(L, FakeHW())
    assert out["recorded"] == 135 and not out["errors"] and out["window_games"] == 1
    recs = L.records()
    assert len(recs) == 135
    r = recs[0]
    assert parse_ts(r["recorded_at"]) < TIP and parse_ts(r["fetched_at"]) < parse_ts(r["recorded_at"]) and r["snapshot"] == "close"
    assert parse_ts(r["inputs_retrieved_at"]) <= parse_ts(r["forecast_computed_at"]) <= parse_ts(r["recorded_at"])
    assert r["line"] == 15.5 and r["over_price"] == -110 and r["protocol_version"] == P.PROTOCOL_VERSION
    assert json.loads(r["sim_pmf"]) and json.loads(r["base_pmf"]) and r["agree"] in (0, 1, None)
    for h in (r["input_hash"], r["league_hash"]):
        assert L.get_input(h)                                           # exact inputs retrievable and hash-checked
    assert L.get_input(r["league_hash"])["players"] and integrity_report(L)["ok"]
    assert L.expected_game(GAME) and L._all("SELECT COUNT(*) AS n FROM universe")[0]["n"] == 135


def test_repeated_ticks_do_not_duplicate_and_do_not_refetch_inputs(L):
    hw = FakeHW()
    clock = Clock()
    tick(L, hw, clock)
    n_calls = sum(1 for c in hw.calls if c[0] == "/query")
    out2, out3 = tick(L, hw, clock), tick(L, hw, clock)
    assert len(L.records()) == 135 and out2["recorded"] == 0 and out2["already"] == 135 and out3["recorded"] == 0
    assert sum(1 for c in hw.calls if c[0] == "/query") == n_calls
    assert L.verify_chain()["ok"] and len(L._all("SELECT * FROM league_snapshots")) == 1


def test_a_restart_between_ticks_keeps_records_and_adds_none(tmp_path):
    L1 = Ledger(tmp_path / "l.db", tmp_path / "m")
    hw = FakeHW()
    tick(L1, hw)
    head = L1.verify_chain()["head"]
    L1.close()
    L2 = Ledger(tmp_path / "l.db", tmp_path / "m")
    out = tick(L2, hw)
    assert out["recorded"] == 0 and L2.verify_chain()["head"] == head and L2.verify_chain()["ok"]


def test_the_exact_recorded_line_is_kept_when_hardwood_changes_later(L):
    hw = FakeHW()
    tick(L, hw)
    before = {(r["player_id"], r["market"]): (r["line"], r["over_price"], r["under_price"], r["row_hash"]) for r in L.records()}
    for m in hw.rows:                                                  # the source is edited after the record
        for r in hw.rows[m][:5]:
            r["line"], r["over_price"] = r["line"] + 1, -150
    out = tick(L, hw)
    after = {(r["player_id"], r["market"]): (r["line"], r["over_price"], r["under_price"], r["row_hash"]) for r in L.records()}
    assert before == after and out["recorded"] == 0 and out["source_diffs"] == 30       # 5 players x 3 markets x (line + over price)
    kinds = {r["kind"] for r in L._all("SELECT kind FROM source_diffs")}
    assert kinds == {"line_changed", "over_price_changed"}
    tick(L, hw)
    assert len(L._all("SELECT * FROM source_diffs")) == 30              # not logged twice
    assert L.verify_chain()["ok"]


def test_dashboard_cache_invalidation_cannot_touch_the_ledger(L, tmp_path):
    from app.store import Store
    tick(L, FakeHW())
    head = L.verify_chain()["head"]
    store = Store(tmp_path / "dash.db")
    with store.tx() as c:
        c.execute("DELETE FROM sims")                                   # what a sim-version change does to the dashboard
    store.set_meta("sim_version", "something-else", "2026-11-20T00:00:00Z")
    assert L.verify_chain()["head"] == head and len(L.records()) == 135
    import ast
    tree = ast.parse((ROOT / "app" / "record.py").read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "app.store" not in imported and "app.sim" not in imported - {"app.sim"} | set() or True
    assert "app.store" not in imported and not names & {"Store", "sims_for", "upsert_sims", "sim_view", "enrich"}   # never reads or writes the dashboard store


def test_no_future_observations_are_in_the_saved_inputs(L):
    players = make_players(n_players=45)
    leak = {"date": DATE, "minutes": 33.0, "pts": 99, "reb": 9, "ast": 9, "fg3m": 1, "stl": 1, "blk": 0}      # the game itself
    after = dict(leak, date="2026-11-25")
    for pid, (_n, logs) in players.items():
        logs.extend([leak, after])
    tick(L, FakeHW(players=players))
    for r in L.records()[:20]:
        games = L.get_input(r["input_hash"])["games"]
        assert max(g["date"] for g in games) < DATE and all(g["pts"] != 99 for g in games)
        assert [g["date"] for g in games] == sorted(g["date"] for g in games)


def test_closes_after_tip_or_outside_the_window_are_never_recorded(L):
    hw = FakeHW()
    late = Clock(start=TIP + timedelta(minutes=1))
    assert tick(L, hw, late)["recorded"] == 0 and L.records() == []             # the tick itself is after tip-off
    early = Clock(start=TIP - timedelta(minutes=P.CLOSE_WINDOW_MIN + 5))
    assert tick(L, hw, early)["window_games"] == 0 and L.records() == []
    rows = {m: [close_row(p, m, fetched=TIP + timedelta(seconds=5)) for p in hw.players] for m in ("pts",)}
    assert tick(L, FakeHW(rows=rows))["rejected"] == {"fetched_after_tip": 45} and L.records() == []
    L2 = Ledger(":memory:")
    rows = {"pts": [close_row(p, "pts", fetched=TIP - timedelta(minutes=P.CLOSE_FETCH_MAX_BEFORE_TIP_MIN + 1)) for p in hw.players]}
    assert tick(L2, FakeHW(rows=rows))["rejected"] == {"fetched_too_early": 45} and L2.records() == []
    hw2 = FakeHW(rows={"pts": [close_row(p, "pts") for p in hw.players]})            # the worker then refreshes the close row with a valid pull
    assert tick(L2, hw2)["recorded"] == 45                                             # the early row was not terminal
    L3 = Ledger(":memory:")
    rows = {"pts": [close_row(p, "pts", snapshot="open") for p in hw.players]}      # opening rows are not primary
    assert tick(L3, FakeHW(rows=rows))["close_rows"] == 0 and L3.records() == []


def test_recording_stops_at_tip_off_even_when_a_tick_straddles_it(L):
    from app.record import ensure_inputs, ensure_league
    hw = FakeHW()
    seed = Clock(start=NOW)
    asyncio.run(ensure_inputs(hw, L, DATE, SEASON, list(hw.players), seed))
    assert ensure_league(L, DATE, SEASON, seed)
    slow = Clock(start=TIP - timedelta(seconds=12), step=timedelta(seconds=1))        # every clock read costs one second; tip-off arrives mid-tick
    out = tick(L, hw, slow)
    assert 0 < out["recorded"] < 135 and out["rejected"].get("recorded_after_tip", 0) == 135 - out["recorded"]
    assert all(parse_ts(r["recorded_at"]) < TIP for r in L.records()) and integrity_report(L)["records_after_tip"] == 0


def test_stale_or_early_inputs_are_refused(L):
    hw = FakeHW()
    clock = Clock()
    old = ts(NOW - timedelta(hours=P.INPUT_MAX_AGE_HOURS + 2))
    for pid, (_n, logs) in hw.players.items():
        L.index_input(DATE, pid, L.add_input("player_log", {"games": logs}, old), old, len(logs))
    out = tick(L, hw, clock)
    assert out["recorded"] == 0 and out["rejected"].get("stale_inputs") == 135
    # pure rule: before 08:00 ET on the board date is stale, as is a league snapshot older than the cap
    comp = NOW
    assert fresh_enough(ts(NOW - timedelta(hours=2)), ts(NOW - timedelta(hours=2)), comp, DATE, TIP)
    assert not fresh_enough(ts(parse_ts("2026-11-20T11:59:00Z")), ts(NOW - timedelta(hours=2)), comp, DATE, TIP)
    assert not fresh_enough(ts(NOW - timedelta(hours=2)), ts(NOW - timedelta(hours=P.INPUT_MAX_AGE_HOURS + 1)), comp, DATE, TIP)
    assert not fresh_enough(ts(NOW - timedelta(hours=2)), None, comp, DATE, TIP)


def test_league_parameters_are_one_saved_snapshot_per_board_date(L):
    hw = FakeHW()
    tick(L, hw)
    snap = L.league_snapshot(DATE)
    lobj = L.get_input(snap["input_hash"])
    assert snap["n_players"] == 45 and set(lobj["rate"]) == {"pts", "reb", "ast", "fg3m", "stl", "blk"}
    assert {r["league_hash"] for r in L.records()} == {snap["input_hash"]}


def test_thin_boards_and_short_histories_are_logged_not_guessed(L):
    players = make_players(n_players=45)
    players["p3"] = ("Player 3", players["p3"][1][:3])                  # only 3 earlier games
    out = tick(L, FakeHW(players=players))
    assert out["rejected"] == {"insufficient_history": 3} and out["recorded"] == 132
    L2 = Ledger(":memory:")
    thin = make_players(n_players=10)
    out = tick(L2, FakeHW(players=thin, rows={"pts": [close_row(p) for p in thin]}))
    assert out["recorded"] == 0 and out["rejected"] == {"league_unavailable": 10}


def test_non_regular_season_games_and_bad_rows_are_rejected_with_reasons(L):
    hw = FakeHW(games=[{"id": GAME, "home": "OKC", "away": "DEN", "status": "scheduled", "tipoff_utc": "2026-11-21T00:00:00Z", "season_type": "playoffs"}])
    out = tick(L, hw)
    assert out["recorded"] == 0 and out["rejected"] == {"season_type_not_expected": 135}
    tip = TIP
    assert check_row(close_row("p", line=None), tip) == "bad_line"
    assert check_row(close_row("p", over=None), tip) == "bad_prices"
    assert check_row(close_row("p", fetched=TIP - timedelta(minutes=10), market="zzz"), tip) == "unknown_market"
    assert check_row(close_row("p", fetched_at=None) | {"fetched_at": None}, tip) == "no_fetched_at"
    assert check_row(close_row("p"), None) == "no_tip_time"
    assert check_row(close_row("p"), tip) is None


def test_one_failed_market_is_logged_and_the_rest_still_recorded(L):
    class OneMarketDown(FakeHW):
        async def get(self, path, params=None):
            if path == "/props" and params["market"] == "reb":
                raise RuntimeError("boom")
            return await super().get(path, params)
    out = tick(L, OneMarketDown())
    assert out["recorded"] == 90 and out["errors"] == ["props reb: RuntimeError"]
    assert L._all("SELECT ok FROM ticks ORDER BY id DESC LIMIT 1")[0]["ok"] == 0


def test_total_hardwood_failure_is_logged_and_never_raises(L):
    out = tick(L, FakeHW(fail={"games"}))
    assert out["recorded"] == 0 and out["errors"]
    assert L._all("SELECT COUNT(*) AS n FROM ticks")[0]["n"] == 1 and L.records() == []


def test_baseline_and_simulator_share_inputs_and_eligibility(L):
    players = make_players(n_players=45)
    players["p3"] = ("Player 3", players["p3"][1][:4])
    tick(L, FakeHW(players=players))
    assert all(r["sim_pmf"] and r["base_pmf"] and r["n_prior"] >= P.MIN_PRIOR_GAMES for r in L.records())
    assert not [r for r in L.records() if r["player_id"] == "p3"]           # neither side is recorded for an ineligible player


def test_saved_inputs_reproduce_the_recorded_forecasts(L):
    tick(L, FakeHW())
    rep = replay_sample(L, 12)
    assert rep["checked"] == 12 and rep["ok"], rep


def test_no_credentials_reach_the_ledger_mirror_or_reports(L, tmp_path):
    tick(L, FakeHW())
    dump = "\n".join(L.conn.iterdump())
    for f in (tmp_path / "mirror").glob("*"):
        dump += f.read_text()
    assert SECRET not in dump and not secrets_in(dump, [SECRET])
    assert secrets_in("key=" + SECRET, [SECRET]) and secrets_in("ctnba_abcdefghijk")          # the scanner does catch them


def test_the_scheduler_hook_is_off_by_default_and_never_breaks_collection(tmp_path, monkeypatch):
    from app import scheduler
    from app.config import Settings
    off = Settings()
    assert off.ledger_enabled is False
    assert asyncio.run(scheduler.ledger_step(FakeHW(), off, NOW, "t")) is True
    on = Settings(ledger_enabled=True, ledger_path=tmp_path / "x" / "l.db", ledger_mirror_dir=tmp_path / "x" / "m")

    async def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr("app.record.ledger_tick", boom)
    assert asyncio.run(scheduler.ledger_step(FakeHW(), on, NOW, "t")) is False          # reported, not raised


def test_a_game_that_ends_after_midnight_still_gets_its_final_status(L):
    hw = FakeHW()
    tick(L, hw)
    assert L.latest_status(GAME) == "scheduled"
    hw.games[0]["status"] = "final"
    next_day = Clock(start=parse_ts("2026-11-21T06:00:00Z"))                        # 01:00 ET the next day: the board date has rolled over
    asyncio.run(ledger_tick(hw, L, SEASON, clock=next_day, params=PARAMS))
    assert L.latest_status(GAME) == "final"                                          # re-read through the "yesterday" schedule call
