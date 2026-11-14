import numpy as np

from scripts.backtest_stage_a2 import (MODELS, analyze, attach_board, auc, build_features, cluster_bootstrap_diff, devig,
                                       forward_chain_masks, fit_logit, format_report, logit, verdict)


def test_devig_removes_the_margin_and_handles_missing_prices():
    assert abs(devig(-110, -110) - 0.5) < 1e-12
    p = devig(-115, -111)
    assert 0.5 < p < 0.51          # the over costs more (-115 vs -111), so the market rates it slightly more likely
    assert abs(devig(+100, -120) - (0.5 / (0.5 + 120 / 220))) < 1e-12
    assert devig(None, -110) is None and devig("x", -110) is None


def test_logistic_fit_recovers_known_weights():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(6000, 2))
    true = np.array([-0.2, 1.0, -0.5])
    y = (rng.random(6000) < 1 / (1 + np.exp(-(true[0] + X @ true[1:])))).astype(float)
    w, _ = fit_logit(X, y, l2=0.01)
    assert np.allclose(w, true, atol=0.12)


def test_forward_chaining_never_trains_on_the_test_dates_or_later():
    dates = np.array([f"2026-11-{d:02d}" for d in range(1, 13) for _ in range(3)])
    splits = forward_chain_masks(dates, blocks=4)
    assert len(splits) == 3
    for tr, te in splits:
        assert not (tr & te).any()
        assert max(dates[tr].tolist()) < min(dates[te].tolist())


def test_cluster_bootstrap_interval_covers_zero_for_equal_models_and_excludes_it_for_a_better_one():
    rng = np.random.default_rng(1)
    cluster = np.repeat(np.arange(400), 3)
    base = rng.random(1200) * 0.3
    d, lo, hi = cluster_bootstrap_diff(base, base, cluster, reps=300)
    assert d == 0 and lo <= 0 <= hi
    d, lo, hi = cluster_bootstrap_diff(base - 0.05, base, cluster, reps=300)
    assert hi < 0 and abs(d + 0.05) < 1e-9


def test_auc_matches_known_values_including_ties():
    y = np.array([0, 0, 1, 1.0])
    assert auc(np.array([0.1, 0.2, 0.8, 0.9]), y) == 1.0
    assert auc(np.array([0.5, 0.5, 0.5, 0.5]), y) == 0.5


def _record(**kw):
    base = {"player_id": "p1", "game_id": "g1", "market": "pts", "date": "2026-11-01", "line": 20.5, "over": 1, "p_sim": 0.6,
            "p_hitrate": 0.5, "sim_mean": 22.0, "sim_sd": 5.0, "min_trend": 1.0, "venue": "home", "rest_days": 1, "started": True}
    base.update(kw)
    return base


def test_attach_board_joins_by_player_game_and_market_and_drops_rows_without_prices():
    board_ok = {"over_price": -115, "under_price": -105, "matchup": {"rank": 20, "of": 30, "vs_avg": 0.8}, "last10": {"over": 6, "under": 4}}
    boards = {("p1", "g1", "pts"): board_ok, ("p2", "g2", "pts"): {"over_price": None, "under_price": -110}}
    recs = [_record(), _record(player_id="p2", game_id="g2"), _record(player_id="p3", game_id="g3")]
    rows, stats = attach_board(recs, boards)
    assert len(rows) == 1 and (stats["no_prices"], stats["no_board_row"]) == (1, 1)
    f = build_features(rows)
    assert 0.0 < f["mu_rank"][0] < 1.0 and f["home"][0] == 1.0 and f["z_sim"][0] == 0.3 and f["y"][0] == 1.0


def test_a_missing_matchup_is_neutral_not_a_crash():
    boards = {("p1", "g1", "pts"): {"over_price": -110, "under_price": -110, "matchup": None, "last10": None}}
    rows, stats = attach_board([_record()], boards)
    f = build_features(rows)
    assert stats["no_matchup"] == 1 and f["mu_rank"][0] == 0.0 and f["mu_vs_avg"][0] == 0.0


def _synthetic(effect_sim, n=4000, seed=2):
    rng = np.random.default_rng(seed)
    p_mkt = np.clip(rng.normal(0.47, 0.04, n), 0.3, 0.7)
    z = rng.normal(size=n)
    lm = logit(p_mkt)
    y = (rng.random(n) < 1 / (1 + np.exp(-(lm + effect_sim * z)))).astype(float)
    noise = lambda: rng.normal(size=n)
    dates = np.array([f"2026-11-{1 + i % 12:02d}" for i in range(n)])
    return {"y": y, "p_mkt": p_mkt, "p_sim": 1 / (1 + np.exp(-(lm + 2.5 * z))), "logit_mkt": lm,
            "logit_sim": logit(1 / (1 + np.exp(-(lm + 2.5 * z)))), "z_sim": z, "mu_rank": noise(), "mu_vs_avg": noise(),
            "home": (noise() > 0).astype(float), "rest0": (noise() > 1).astype(float), "rest_long": (noise() > 1).astype(float),
            "started": (noise() > -1).astype(float), "min_trend": noise(), "hr_prior": noise(), "b_last10": noise(),
            "date": dates, "cluster": np.arange(n)}


def test_a_real_signal_is_found_and_noise_inputs_are_not_rewarded():
    res = analyze(_synthetic(effect_sim=0.6), reps=300)
    row = {r["model"]: r for r in res["table"]}
    assert verdict(row["market + sim"]) == "better"
    # pure noise columns on top must not look meaningfully better than the model without them
    assert row["market + everything"]["brier"] > row["market + sim"]["brier"] - 0.002
    assert res["n_test"] > 1000 and len(res["coefs_full_fit"]) == len(MODELS[-1][1]) + 1


def test_no_signal_means_no_model_beats_the_calibrated_market():
    res = analyze(_synthetic(effect_sim=0.0), reps=300)
    for r in res["table"]:
        if r["model"].startswith("market +"):
            assert verdict(r) != "better", r
    text = format_report({"players_loaded": 1, "games_loaded": 1}, {"board_rows": 1, "records": 1, "kept": 1, "no_board_row": 0,
                                                                    "no_prices": 0, "no_matchup": 0, "venue_values": {}}, res)
    assert "BEST MODEL" in text and "COEFFICIENTS" in text
