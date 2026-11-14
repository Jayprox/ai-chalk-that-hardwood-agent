import asyncio

import numpy as np

from scripts.backtest_stage_a import (BOARD_MARKETS, League, Params, actual_for, brier, calibration, check_results, gather,
                                      league_params, outcome, prob_over, run_backtest, simulate_player, summarize)


def make_players(n_players=60, n_games=14, seed=3, line=15.5, shift=0.0):
    """Synthetic league: each player has his own points-per-minute rate; the line is the same for everyone."""
    rng = np.random.default_rng(seed)
    players = {}
    for i in range(n_players):
        rate = 0.3 + 0.4 * (i / max(n_players - 1, 1))
        logs = []
        for d in range(n_games):
            minutes = float(np.clip(rng.normal(30, 4), 12, 44))
            mu = rate * minutes + shift
            r = 10.0
            pts = int(rng.negative_binomial(r, r / (r + mu)))
            logs.append({"date": f"2026-11-{d + 1:02d}", "minutes": minutes, "pts": pts, "reb": 4, "ast": 3, "fg3m": 1, "stl": 1,
                         "blk": 0, "props": {"pts": {"line": line, "result": outcome(pts, line)}}})
        players[f"p{i}"] = (f"Player {i}", logs)
    return players


def test_actual_and_outcome_for_single_and_combo_markets():
    g = {"pts": 20, "reb": 5, "ast": 7, "stl": 1, "blk": 2}
    assert actual_for("pts", g) == 20 and actual_for("pra", g) == 32 and actual_for("stocks", g) == 3
    assert actual_for("pa", {"pts": 20}) is None and actual_for("nonsense", g) is None
    assert (outcome(21, 20.5), outcome(20, 20.5), outcome(20, 20)) == ("over", "under", "push")


def test_integrity_check_flags_a_stored_result_that_disagrees_with_the_stat():
    players = {"a": ("A", [
        {"date": "d1", "minutes": 30, "pts": 25, "props": {"pts": {"line": 24.5, "result": "over"}}},
        {"date": "d2", "minutes": 30, "pts": 20, "props": {"pts": {"line": 24.5, "result": "over"}}},      # wrong: 20 < 24.5
        {"date": "d3", "minutes": 0, "pts": 0, "props": {"pts": {"line": 24.5, "result": "under"}}},       # did not play
        {"date": "d4", "minutes": 30, "pts": 5, "props": {"pts": {"line": 4.5, "result": None}}},          # no stored result
    ])}
    r = check_results(players)
    assert (r["checked"], r["mismatched"], r["no_stored_result"], r["props_on_games_not_played"]) == (2, 1, 1, 1)
    assert r["samples"][0]["recomputed"] == "under" and r["samples"][0]["hardwood_result"] == "over"


def test_a_prediction_uses_only_earlier_games():
    lg = League(rate={k: 0.2 for k in ("pts", "reb", "ast", "fg3m", "stl", "blk")}, alpha={k: 0.1 for k in ("pts", "reb", "ast", "fg3m", "stl", "blk")})
    prior = make_players(1, 8)["p0"][1]
    a = simulate_player(prior, lg, Params(draws=500), np.random.default_rng(1))
    later = prior + [{"date": "2026-12-01", "minutes": 40.0, "pts": 90, "reb": 0, "ast": 0, "fg3m": 0, "stl": 0, "blk": 0}]
    b = simulate_player(prior, lg, Params(draws=500), np.random.default_rng(1))
    assert np.array_equal(a["pts"], b["pts"])
    c = simulate_player(later, lg, Params(draws=500), np.random.default_rng(1))
    assert c["pts"].mean() > a["pts"].mean()  # the extra game only matters once it is in the prior


def test_too_few_earlier_games_gives_no_prediction():
    lg = league_params(l for _n, l in make_players(5, 10).values())
    assert simulate_player(make_players(1, 4)["p0"][1], lg, Params(), np.random.default_rng(0)) is None


def test_push_probability_is_removed_from_whole_number_lines():
    draws = np.array([9, 10, 10, 10, 11, 12, 8, 10], dtype=float)  # 4 pushes of 8
    assert prob_over(draws, 10) == (2 / 8) / (1 - 4 / 8)
    assert prob_over(np.array([10.0, 10.0]), 10) is None


def test_run_backtest_skips_pushes_and_the_first_games():
    players = make_players(8, 10, line=15.0)
    recs = run_backtest(players, Params(draws=300))
    assert recs and all(r["prior_games"] >= 5 for r in recs)
    assert all(r["actual"] != r["line"] for r in recs)  # a push is not an outcome
    assert all(0.0 <= r["p_sim"] <= 1.0 and 0.0 < r["p_hitrate"] < 1.0 for r in recs)


def test_simulation_beats_fifty_percent_and_is_calibrated_on_data_it_matches():
    recs = run_backtest(make_players(60, 14), Params(draws=800))
    s = summarize(recs)
    assert s["n"] > 400
    assert s["brier"]["sim"] < s["brier"]["fifty_percent"] - 0.02       # real signal: rates differ and the line is flat
    assert abs(s["mean_p_sim"] - s["over_rate"]) < 0.05                  # no overall bias
    assert s["strong_over"]["hit_rate"] > 0.6 and s["strong_under"]["hit_rate"] > 0.6


def test_a_line_set_at_the_true_median_gets_no_edge():
    # Lines move with each player's own rate, so the market is as informed as the model: Brier should sit near 0.25.
    rng = np.random.default_rng(5)
    players = {}
    for i in range(50):
        rate = 0.3 + 0.4 * (i / 49)
        logs = []
        for d in range(14):
            minutes = float(np.clip(rng.normal(30, 4), 12, 44))
            mu = rate * minutes
            pts = int(rng.negative_binomial(10, 10 / (10 + mu)))
            line = round(rate * 30) + 0.5
            logs.append({"date": f"2026-11-{d + 1:02d}", "minutes": minutes, "pts": pts, "reb": 4, "ast": 3, "fg3m": 1, "stl": 1, "blk": 0,
                         "props": {"pts": {"line": line, "result": outcome(pts, line)}}})
        players[f"p{i}"] = (f"P{i}", logs)
    s = summarize(run_backtest(players, Params(draws=800)))
    assert abs(s["brier"]["sim"] - 0.25) < 0.03


def test_calibration_table_counts_every_record_once():
    recs = [{"p_sim": p, "over": o} for p, o in [(0.1, 0), (0.33, 0), (0.42, 1), (0.47, 0), (0.52, 1), (0.58, 1), (0.65, 1), (0.9, 1)]]
    rows = calibration(recs)
    assert sum(r["n"] for r in rows) == len(recs)
    assert brier([0.5, 0.5], [1, 0]) == 0.25


class FakeHW:
    def __init__(self):
        self.players = make_players(4, 8)

    async def get(self, path, params=None):
        assert path == "/props"
        if params["market"] == "pts":
            return {"data": [{"name": f"Player {i}", "team": "OKC", "player_id": f"p{i}"} for i in range(4)]}
        return {"data": []}

    async def post(self, path, body):
        assert path == "/query" and body["scope"] == "game_log"
        return {"data": self.players[body["id"]][1]}

    async def resolve_player(self, name):
        raise AssertionError("ids come from the board rows")


def test_gather_loads_each_player_on_the_boards_once():
    players, info = asyncio.run(gather(FakeHW(), "2026-11-13", "2026-27"))
    assert info["players_loaded"] == 4 and info["games_loaded"] == 32 and info["board_markets"]["pts"] == 4
    assert set(BOARD_MARKETS) == set(info["board_markets"])
    assert not info["errors"]
