import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.collector import collect_once
from app.sim import enrich, pmf_from_draws, pmf_to_over_prob, sim_view
from app.store import Store
from tests.test_backtest import make_players
from tests.test_collector import G1, G2, FakeHW, row

NOW = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc)
DATE = "2026-11-20"           # after every synthetic game log date (2026-11-01 .. 2026-11-14)


class LogHW:
    """Hardwood stand-in that serves game logs for /query."""
    def __init__(self, players, fail=()):
        self.players, self.fail, self.calls = players, set(fail), []

    async def post(self, path, body):
        self.calls.append((path, body["id"]))
        if body["id"] in self.fail:
            raise RuntimeError("boom")
        return {"data": self.players[body["id"]][1] if body["id"] in self.players else []}


def seeded(tmp_path, n=45, pts_markets=("pts",), games=None):
    store = Store(tmp_path / "sim.db")
    rows = [row(f"p{i}", game=G1, name=f"Player {i}", line=15.5) for i in range(n)]
    hw = FakeHW({m: rows for m in pts_markets}, games=games)
    asyncio.run(collect_once(hw, store, DATE, NOW))
    return store


def test_pmf_helpers_remove_pushes_and_handle_edges():
    pmf = [0.1, 0.2, 0.4, 0.2, 0.1]
    assert pmf_to_over_prob(pmf, 2.5) == pytest.approx(0.3)
    assert pmf_to_over_prob(pmf, 2) == pytest.approx(0.3 / 0.6)          # the push mass at 2 is removed
    assert pmf_to_over_prob(pmf, 10.5) == 0.0 and pmf_to_over_prob([], 2.5) is None and pmf_to_over_prob(pmf, "x") is None
    assert pmf_to_over_prob([0, 0, 1.0], 2) is None                       # all mass on a push
    p = pmf_from_draws([0, 1, 1, 2, 2, 2, 2, 3])
    assert p == [0.125, 0.25, 0.5, 0.125]


def test_sim_view_statuses():
    ok = {"status": "ok", "pmf": json.dumps([0.0, 0.0, 1.0]), "mean": 2.0, "sd": 0.0, "n_prior": 9}
    assert sim_view(None, 1.5, "OVER")["status"] == "pending"
    assert sim_view({"status": "none", "pmf": None, "n_prior": 2}, 1.5, "OVER")["status"] == "none"
    assert sim_view(ok, 1.5, "OVER")["status"] == "agree" and sim_view(ok, 1.5, "UNDER")["status"] == "disagree"
    v = sim_view(ok, 1.5, "OVER")
    assert v["p_over_raw"] == 1.0 and 0.5 < v["p_over"] < 0.955


def test_enrich_stores_distributions_once_and_is_idempotent(tmp_path):
    store = seeded(tmp_path)
    hw = LogHW(make_players(n_players=45))
    out = asyncio.run(enrich(hw, store, DATE, "2026-27", NOW))
    assert out["pairs"] == 45 and out["sims"] == 45 and out["players_loaded"] == 45 and not out["errors"]
    sims = store.sims_for(DATE)
    assert len(sims) == 45
    s0 = sims[("p0", G1, "pts")]
    pmf = json.loads(s0["pmf"])
    assert s0["status"] == "ok" and s0["n_prior"] == 14 and sum(pmf) == pytest.approx(1.0, abs=0.01) and s0["mean"] > 0
    assert store.get_meta("league:2026-27")["players"] == 45
    calls = len(hw.calls)
    again = asyncio.run(enrich(hw, store, DATE, "2026-27", NOW))
    assert again["pairs"] == 0 and len(hw.calls) == calls                 # nothing to do, no new calls


def test_enrich_is_reproducible(tmp_path):
    a = seeded(tmp_path)
    players = make_players(n_players=45)
    asyncio.run(enrich(LogHW(players), a, DATE, "2026-27", NOW))
    first = {k: v["pmf"] for k, v in a.sims_for(DATE).items()}
    with a.tx() as c:
        c.execute("DELETE FROM sims")
    asyncio.run(enrich(LogHW(players), a, DATE, "2026-27", NOW))
    assert {k: v["pmf"] for k, v in a.sims_for(DATE).items()} == first


def test_players_with_too_few_earlier_games_get_status_none(tmp_path):
    store = seeded(tmp_path)
    players = make_players(n_players=45)
    players["p0"] = ("Player 0", players["p0"][1][:3])
    asyncio.run(enrich(LogHW(players), store, DATE, "2026-27", NOW))
    assert store.sims_for(DATE)[("p0", G1, "pts")]["status"] == "none"


def test_only_games_dated_before_the_board_date_are_used(tmp_path):
    store = seeded(tmp_path)
    players = make_players(n_players=45)
    store2 = Store(tmp_path / "other.db")
    asyncio.run(collect_once(FakeHW({"pts": [row(f"p{i}", game=G1, line=15.5) for i in range(45)]}), store2, "2026-11-08", NOW))
    asyncio.run(enrich(LogHW(players), store2, "2026-11-08", "2026-27", NOW))
    assert store2.sims_for("2026-11-08")[("p0", G1, "pts")]["n_prior"] == 7        # logs 2026-11-01..07


def test_started_games_are_skipped(tmp_path):
    games = [{"id": G1, "home": "OKC", "away": "DEN", "status": "in_progress", "tipoff_utc": "2026-11-13T19:00:00Z"}]
    store = seeded(tmp_path, games=games)
    hw = LogHW(make_players(n_players=45))
    out = asyncio.run(enrich(hw, store, DATE, "2026-27", NOW))
    assert out["pairs"] == 0 and out["skipped_started"] == 45 and hw.calls == []


def test_combo_markets_sum_the_stat_draws(tmp_path):
    store = seeded(tmp_path, pts_markets=("pts", "reb", "pr"))
    asyncio.run(enrich(LogHW(make_players(n_players=45)), store, DATE, "2026-27", NOW))
    s = store.sims_for(DATE)
    pts, reb, pr = (s[("p5", G1, m)]["mean"] for m in ("pts", "reb", "pr"))
    assert pr == pytest.approx(pts + reb, abs=0.05)


def test_one_failed_game_log_does_not_block_the_rest_and_retries_later(tmp_path):
    store = seeded(tmp_path)
    players = make_players(n_players=45)
    out = asyncio.run(enrich(LogHW(players, fail={"p3"}), store, DATE, "2026-27", NOW))
    assert out["sims"] == 44 and len(out["errors"]) == 1 and ("p3", G1, "pts") not in store.sims_for(DATE)
    out2 = asyncio.run(enrich(LogHW(players), store, DATE, "2026-27", NOW))
    assert out2["pairs"] == 1 and ("p3", G1, "pts") in store.sims_for(DATE)


def test_small_boards_use_stored_league_parameters_or_skip(tmp_path):
    store = seeded(tmp_path, n=3)
    out = asyncio.run(enrich(LogHW(make_players(n_players=3)), store, DATE, "2026-27", NOW))
    assert out["sims"] == 0 and out["warnings"] and store.sims_for(DATE) == {}
    store.set_meta("league:2026-27", {"rate": {k: 0.3 for k in ("pts", "reb", "ast", "fg3m", "stl", "blk")},
                                      "alpha": {k: 0.2 for k in ("pts", "reb", "ast", "fg3m", "stl", "blk")}, "players": 99, "as_of": "2026-11-10"}, "x")
    out = asyncio.run(enrich(LogHW(make_players(n_players=3)), store, DATE, "2026-27", NOW))
    assert out["sims"] == 3 and out["league"].startswith("stored")


def test_scheduler_tick_enriches_after_a_good_collection_and_survives_a_sim_failure(tmp_path):
    from app.scheduler import Schedule, tick
    store = Store(tmp_path / "t.db")

    class Both(FakeHW):
        async def post(self, path, body):
            return await LogHW(make_players(n_players=45)).post(path, body)

    rows = [row(f"p{i}", game=G1, line=15.5) for i in range(45)]
    hw = Both({"pts": rows})
    res = asyncio.run(tick(hw, store, NOW, Schedule(), "2026-27", True))
    assert res and res[0]["ok"] and "sim" in res[0]

    class Broken(FakeHW):
        async def post(self, path, body):
            raise RuntimeError("down")

    store2 = Store(tmp_path / "t2.db")
    res = asyncio.run(tick(Broken({"pts": rows}), store2, NOW, Schedule(), "2026-27", True))
    assert res[0]["ok"] and res[0]["sim"]["errors"]


def test_a_new_model_version_recomputes_stored_distributions(tmp_path):
    store = seeded(tmp_path)
    players = make_players(n_players=45)
    asyncio.run(enrich(LogHW(players), store, DATE, "2026-27", NOW))
    first = store.sims_for(DATE)[("p0", G1, "pts")]["computed_at"]
    again = asyncio.run(enrich(LogHW(players), store, DATE, "2026-27", NOW))
    assert again["pairs"] == 0                                              # same version: nothing to do
    store.set_meta("sim_version", "old-model", "x")                         # as if the fit file had changed
    later = datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)
    redo = asyncio.run(enrich(LogHW(players), store, DATE, "2026-27", later))
    assert redo["pairs"] == 45 and store.sims_for(DATE)[("p0", G1, "pts")]["computed_at"] != first


def test_game_log_order_does_not_change_the_simulation_or_the_stored_distribution(tmp_path):
    """Hardwood returns game logs newest first. The minutes model's recent-trend feature must see them oldest first either way."""
    import numpy as np
    from app import minutes_model as mm
    from app.simulation import Params, league_params, simulate_player
    players = make_players(n_players=45)
    logs = sorted(players["p3"][1], key=lambda g: g["date"])
    model = mm.MinutesModel(mean=[0.5, 0, 5, 9, 3], scale=[1, 4, 2, 2, 1], coef=[0, 3.0, 0, 0, 0], intercept=0.0)   # trend-driven adjustment
    lg = league_params(l for _n, l in players.values())
    a = simulate_player(logs, lg, Params(draws=500, minutes=model), np.random.default_rng(1))
    b = simulate_player(list(reversed(logs)), lg, Params(draws=500, minutes=model), np.random.default_rng(1))
    for k in a:
        assert np.array_equal(a[k], b[k])
    fwd = {pid: (n, sorted(l, key=lambda g: g["date"])) for pid, (n, l) in players.items()}
    rev = {pid: (n, list(reversed(sorted(l, key=lambda g: g["date"])))) for pid, (n, l) in players.items()}
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    sa, sb = seeded(tmp_path / "a"), seeded(tmp_path / "b")
    asyncio.run(enrich(LogHW(fwd), sa, DATE, "2026-27", NOW, params=Params(draws=500, minutes=model)))
    asyncio.run(enrich(LogHW(rev), sb, DATE, "2026-27", NOW, params=Params(draws=500, minutes=model)))
    a_, b_ = ({k: v["pmf"] for k, v in st.sims_for(DATE).items()} for st in (sa, sb))
    assert a_ and a_ == b_
