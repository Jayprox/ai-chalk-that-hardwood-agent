import numpy as np
import pytest

from app.simulation import League, Params, simulate_player
from scripts.backtest_blend import compare, slope
from scripts.backtest_stage_a import run_backtest
from tests.test_backtest import make_players


def lg():
    return League(rate={k: 0.3 for k in ("pts", "reb", "ast", "fg3m", "stl", "blk")}, alpha={k: 0.2 for k in ("pts", "reb", "ast", "fg3m", "stl", "blk")})


def logs(n, pts_per_min, minutes=30.0):
    return [{"date": f"d{i:02d}", "minutes": minutes, "pts": pts_per_min * minutes, "reb": 4, "ast": 3, "fg3m": 1, "stl": 1, "blk": 0} for i in range(n)]


def test_no_last_season_or_cap_zero_changes_nothing():
    prior = logs(6, 0.5)
    a = simulate_player(prior, lg(), Params(), np.random.default_rng(1))
    b = simulate_player(prior, lg(), Params(last_cap_minutes=600), np.random.default_rng(1), None)
    c = simulate_player(prior, lg(), Params(last_cap_minutes=600), np.random.default_rng(1), [])
    d = simulate_player(prior, lg(), Params(last_cap_minutes=0), np.random.default_rng(1), logs(60, 0.9))
    for k in a:
        assert np.array_equal(a[k], b[k]) and np.array_equal(a[k], c[k]) and np.array_equal(a[k], d[k])


def test_last_season_moves_the_estimate_toward_the_players_own_rate():
    prior = logs(6, 0.3)                                    # this season looks league-average
    base = simulate_player(prior, lg(), Params(draws=20000), np.random.default_rng(1))["pts"].mean()
    blend = simulate_player(prior, lg(), Params(draws=20000, last_cap_minutes=600), np.random.default_rng(1), logs(70, 0.6))["pts"].mean()
    assert blend > base * 1.3                               # a 0.6/min player last year pulls the estimate up
    low = simulate_player(prior, lg(), Params(draws=20000, last_cap_minutes=600), np.random.default_rng(1), logs(70, 0.1))["pts"].mean()
    assert low < base * 0.8


def test_the_cap_limits_how_much_last_season_can_count():
    prior = logs(6, 0.3)
    small = simulate_player(prior, lg(), Params(draws=20000, last_cap_minutes=100), np.random.default_rng(1), logs(70, 0.6))["pts"].mean()
    big = simulate_player(prior, lg(), Params(draws=20000, last_cap_minutes=1200), np.random.default_rng(1), logs(70, 0.6))["pts"].mean()
    assert big > small


def test_slope_is_one_for_a_calibrated_forecast_and_low_for_an_overconfident_one():
    rng = np.random.default_rng(0)
    p = rng.uniform(0.2, 0.8, 20000)
    y = (rng.uniform(size=p.size) < p).astype(float)
    assert slope(p, y) == pytest.approx(1.0, abs=0.12)
    over = 1 / (1 + np.exp(-3 * np.log(p / (1 - p))))
    assert slope(over, y) < 0.5


def test_compare_pairs_records_and_reports_intervals():
    players = {pid: (n, [dict(g, game_id=f"g{pid}{g['date']}") for g in lg_]) for pid, (n, lg_) in make_players(n_players=20, n_games=14).items()}
    last = {pid: [dict(g, date=f"2025-{i:02d}") for i, g in enumerate(logs_)] for pid, (_n, logs_) in players.items()}
    recs = {cap: run_backtest(players, Params(draws=300, last_cap_minutes=cap), last=last) for cap in (0.0, 600.0)}
    res = compare(recs)
    assert res["n"] > 100 and set(res["variants"]) == {"0.0", "600.0"}
    d = res["variants"]["600.0"]["brier_diff_vs_current"]
    assert d["lo"] <= d["diff"] <= d["hi"]
