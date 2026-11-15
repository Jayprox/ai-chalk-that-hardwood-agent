import numpy as np
import pytest

from app.simulation import Params, league_params
from scripts.habit_screen import GROUPS, build_rows, ridge_predict, screen


def synth(n_players=40, n_games=60, b2b_effect=-4.0, seed=2):
    """Players whose points drop by `b2b_effect` on back-to-back nights, a fact the baseline cannot know."""
    rng = np.random.default_rng(seed)
    out = {}
    for i in range(n_players):
        rate = 0.3 + 0.3 * i / n_players
        logs = []
        for d in range(n_games):
            b2b = d % 5 == 4
            minutes = float(np.clip(rng.normal(30, 3), 15, 42))
            pts = max(0.0, rate * minutes + (b2b_effect if b2b else 0.0) + rng.normal(0, 3))
            logs.append({"date": f"2026-01-{d + 1:02d}" if d < 31 else f"2026-02-{d - 30:02d}", "minutes": minutes, "pts": round(pts), "reb": 4, "ast": 3, "fg3m": 1,
                         "stl": 1, "blk": 0, "venue": "home" if d % 2 else "away", "opponent": f"T{d % 7}", "player_b2b_night": b2b,
                         "rest_days": 0 if b2b else 2, "player_rest_days": 0 if b2b else 2, "plus_minus": rng.normal(0, 8), "pf": 2})
        out[f"p{i}"] = (f"P{i}", logs)
    return out


def test_ridge_recovers_a_linear_miss_and_ignores_empty_features():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(500, 2))
    y = 2.0 * X[:, 0] + rng.normal(0, 0.1, 500)
    p = ridge_predict(X[:400], y[:400], X[400:], lam=1.0)
    assert np.mean((p - y[400:]) ** 2) < 0.1
    assert np.all(ridge_predict(np.zeros((10, 0)), np.ones(10), np.zeros((3, 0))) == 0)


def test_features_use_only_earlier_games_and_rows_start_after_the_prior_minimum():
    players = synth(n_players=3, n_games=12)
    rows = build_rows(players, league_params(l for _n, l in players.values()), Params(), min_prior=6)
    assert len(rows) == 3 * 6
    r0 = next(r for r in rows if r["pid"] == "p0" and r["date"] == "2026-01-07")
    logs = sorted(players["p0"][1], key=lambda g: g["date"])
    assert r0["base"]["min"] == pytest.approx(np.mean([g["minutes"] for g in logs[:6]]))      # the 7th game is not in its own baseline
    first_day = min(r["date"] for r in rows)
    assert all(r["f"]["opp_pts"] == 0.0 for r in rows if r["date"] == first_day)               # nothing earlier to learn from


def test_the_screen_finds_a_real_back_to_back_effect_and_not_a_fake_one():
    players = synth()
    lg = league_params(l for _n, l in players.values())
    rows = build_rows(players, lg, Params(), coverage_draws=200)
    res = screen(rows, blocks=3, reps=300)
    pts = res["targets"]["pts"]["groups"]
    assert pts["schedule"]["verdict"] == "helps" and pts["schedule"]["pct"] < -3
    assert pts["head_to_head"]["verdict"] != "helps"                                          # head-to-head is pure noise here
    assert set(GROUPS) | {"all"} == set(pts) and set(res["targets"]) == {"min", "pts", "reb", "ast", "fg3m"}
    assert 0.5 < res["coverage"]["pts"]["central80"] < 1.0
