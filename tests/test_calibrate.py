import numpy as np
import pytest

from app.calibration import apply, logit
from scripts.backtest_calibrate import fit, forward_chain, predict
from scripts.backtest_stage_a import Params, run_backtest
from tests.test_backtest import make_players


def test_apply_shrinks_toward_half_and_keeps_direction():
    assert apply(None, 0, 0.2) is None
    assert apply(0.5, 0, 0.2) == pytest.approx(0.5)
    hi, lo = apply(0.8, 0, 0.2), apply(0.2, 0, 0.2)
    assert 0.5 < hi < 0.58 and 0.42 < lo < 0.5 and hi + lo == pytest.approx(1.0)
    assert apply(0.8, 0, 1.0) == pytest.approx(0.8) and logit(0.5) == 0


def test_fit_recovers_a_known_slope_with_and_without_an_intercept():
    rng = np.random.default_rng(1)
    p = rng.uniform(0.15, 0.85, 40000)
    true = 1 / (1 + np.exp(-(0.3 * np.log(p / (1 - p)))))
    y = (rng.uniform(size=p.size) < true).astype(float)
    assert fit(p, y, False)[1] == pytest.approx(0.3, abs=0.05)
    a, b = fit(p, y, True)
    assert abs(a) < 0.05 and b == pytest.approx(0.3, abs=0.06)
    assert np.all(np.sign(predict(p, 0.0, 0.3) - 0.5) == np.sign(p - 0.5))


def test_forward_chain_scores_later_dates_only_and_reports_folds():
    players = {pid: (n, [dict(g, game_id=f"{pid}{g['date']}") for g in lg]) for pid, (n, lg) in make_players(n_players=40, n_games=14).items()}
    recs = run_backtest(players, Params(draws=300))
    res = forward_chain(recs, blocks=3, reps=200)
    assert res["n_test"] > 100 and res["folds"] and set(res["scores"]) == {"raw", "slope_only", "slope_intercept", "fifty"}
    d = res["scores"]["slope_only"]["vs_raw"]
    assert d["lo"] <= d["diff"] <= d["hi"] and res["final"]["slope_only"]["a"] == 0.0


def test_a_typical_raw_probability_is_shrunk_close_to_half():
    from app.calibration import SLOPE
    assert 0.5 < apply(0.69, 0.0, SLOPE) < 0.56 and 0.44 < apply(0.31, 0.0, SLOPE) < 0.5
