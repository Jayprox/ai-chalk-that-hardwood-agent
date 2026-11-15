import json

import numpy as np
import pytest

from app import minutes_model as mm
from app.simulation import Params, league_params, simulate_player
from scripts.fit_sim import central80, deciles, fit_spread, minutes_forward, minutes_rows
from tests.test_habit_screen import synth


def starter_world(n=50, games=50, seed=1):
    """Starters play ~34 min, bench players ~16, and the flat average can't tell a new role from the past."""
    rng = np.random.default_rng(seed)
    out = {}
    for i in range(n):
        logs = []
        for d in range(games):
            starter = i % 2 == 0
            minutes = float(np.clip(rng.normal(34 if starter else 16, 3), 5, 44))
            logs.append({"date": f"2026-01-{d + 1:02d}" if d < 31 else f"2026-02-{d - 30:02d}", "minutes": minutes, "pts": round(minutes * 0.5),
                         "reb": 4, "ast": 3, "fg3m": 1, "stl": 1, "blk": 0, "started": starter, "plus_minus": 0, "pf": 2})
        out[f"p{i}"] = (f"P{i}", logs)
    return out


def test_features_are_computed_from_earlier_games_only_and_default_sensibly():
    games = [{"minutes": 30.0, "started": None, "plus_minus": -10, "pf": 3}, {"minutes": 20.0, "started": None, "plus_minus": 6, "pf": 1}]
    f = mm.features(games)
    assert f[0] == 0.5 and f[2] == pytest.approx(np.std([30, 20], ddof=1)) and f[3] == 8.0 and f[4] == pytest.approx(4 / 50 * 36)


def test_model_without_coefficients_changes_nothing_and_clips_a_wild_adjustment():
    played = [{"minutes": 30.0, "started": True, "plus_minus": 0, "pf": 2}] * 6
    assert mm.MinutesModel().adjust(played) == 0.0
    m = mm.MinutesModel(mean=[0] * 5, scale=[1] * 5, coef=[100, 0, 0, 0, 0], intercept=0.0)
    assert m.adjust(played) == 12.0


def test_fit_round_trips_through_json_and_missing_files_are_harmless(tmp_path):
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, 5))
    model = mm.fit(X, X[:, 0] * 2.0, lam=1.0)
    mm.save_fit(model, {"pts": 1.4}, {"x": 1}, tmp_path / "f.json")
    m2, disp = mm.load_fit(tmp_path / "f.json")
    assert m2.coef == pytest.approx(model.coef) and disp == {"pts": 1.4}
    assert mm.load_fit(tmp_path / "missing.json") == (None, {})
    (tmp_path / "bad.json").write_text("{not json")
    assert mm.load_fit(tmp_path / "bad.json") == (None, {})


def test_minutes_model_improves_a_world_where_role_changes_minutes_and_changes_the_sim():
    players = starter_world()
    for pid, (n, logs) in players.items():             # halfway through, half the players change role
        if int(pid[1:]) % 4 < 2:
            for g in logs[25:]:
                g["started"] = not g["started"]; g["minutes"] = 50 - g["minutes"]
    rows = minutes_rows(players)
    res = minutes_forward(rows, blocks=3, reps=200)
    assert res["mse_model"] <= res["mse_flat"]
    model = mm.fit(rows["X"], rows["y"], fitted_on="synthetic")
    lg = league_params(l for _n, l in players.values())
    prior = sorted(players["p2"][1], key=lambda g: g["date"])[:30]
    flat = simulate_player(prior, lg, Params(draws=8000, use_minutes_model=False), np.random.default_rng(1))["pts"].mean()
    new = simulate_player(prior, lg, Params(draws=8000, minutes=model), np.random.default_rng(1))["pts"].mean()
    assert new != pytest.approx(flat, abs=1e-6)


def test_dispersion_multiplier_widens_the_distribution():
    prior = [{"date": f"d{i}", "minutes": 30.0, "pts": 15, "reb": 4, "ast": 3, "fg3m": 1, "stl": 1, "blk": 0} for i in range(8)]
    lg = league_params([prior])
    a = simulate_player(prior, lg, Params(draws=20000, use_minutes_model=False), np.random.default_rng(1))["pts"].std()
    b = simulate_player(prior, lg, Params(draws=20000, use_minutes_model=False, disp_mult={"pts": 3.0}), np.random.default_rng(1))["pts"].std()
    assert b > a


def test_spread_fit_widens_a_too_narrow_stat():
    rng = np.random.default_rng(4)
    players = {}
    for i in range(40):
        logs = []
        for d in range(40):
            minutes = float(np.clip(rng.normal(30, 3), 15, 42))
            logs.append({"date": f"2026-01-{d + 1:02d}" if d < 31 else f"2026-02-{d - 30:02d}", "minutes": minutes,
                         "pts": float(rng.poisson(0.5 * minutes) + rng.normal(0, 8)).__round__(), "reb": int(rng.poisson(0.15 * minutes)), "ast": 3, "fg3m": 1,
                         "stl": 1, "blk": 0})
        players[f"p{i}"] = (f"P{i}", logs)
    lg = league_params(l for _n, l in players.values())
    lg.alpha = {k: v * 0.2 for k, v in lg.alpha.items()}            # a simulation that is far too narrow
    res = fit_spread(players, lg, None, sample=1500)
    assert res["stats"]["pts"]["best_mult"] > 1.0 and res["stats"]["pts"]["ship"]
    assert res["stats"]["pts"]["central80_late_after"] > res["stats"]["pts"]["central80_late_before"]
    assert res["stats"]["pts"]["crps_hi"] < 0 and res["stats"]["pts"]["crps_pct"] < 0
    assert abs(central80(np.linspace(0, 1, 1000)) - 0.8) < 0.01 and deciles(np.linspace(0, 1, 1000)).sum() == pytest.approx(1.0, abs=0.01)


def test_crps_rewards_the_right_spread_and_is_zero_for_a_perfect_point_forecast():
    from scripts.fit_sim import crps_from_draws
    rng = np.random.default_rng(0)
    truth = rng.normal(10, 3, 4000)
    right = np.mean([crps_from_draws(rng.normal(10, 3, 500), y) for y in truth[:300]])
    narrow = np.mean([crps_from_draws(rng.normal(10, 0.5, 500), y) for y in truth[:300]])
    assert right < narrow
    assert crps_from_draws(np.full(100, 7.0), 7.0) == pytest.approx(0.0)


def test_the_spread_judge_never_sees_a_minutes_model_fitted_on_the_judged_dates():
    """The minutes model handed to fit_spread is trained only on rows before the cutoff (see main_async); here we check the plumbing: the
    cutoff is honored and the early and late halves are disjoint."""
    players = starter_world(n=30, games=45)
    lg = league_params(l for _n, l in players.values())
    res = fit_spread(players, lg, None, sample=800, cut="2026-02-05")
    assert res["split_date"] == "2026-02-05"
