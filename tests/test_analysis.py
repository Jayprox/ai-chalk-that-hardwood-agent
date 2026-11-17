import math

import numpy as np
import pytest

from app import protocol as P
from app.analysis import (Data, crps_pmf, decide_track_a, decide_track_b, gap, interval, pool_market, run_primary, s1_stat)
from app.settle import settled_rows  # noqa: F401  (import check)

MARKETS = ["pts", "reb", "ast", "pra", "pa", "pr", "ra", "fg3m", "stl", "blk"]


def pois(m, top=70):
    p = [math.exp(-m)]
    for k in range(1, top):
        p.append(p[-1] * m / k)
    return p


def row(pid="p", gid="g", market="pts", p_fav=0.6, hit=1, agree=1, side="over", status="settled", actual=5, sim=None, base=None, price=-110):
    pov = p_fav if side == "over" else 1 - p_fav
    other = "under" if side == "over" else "over"
    return {"player_id": pid, "game_id": gid, "market": market, "status": status, "actual": actual,
            "outcome": (side if hit else other) if status == "settled" else None, "market_p_over": pov, "market_side": side,
            "over_price": price, "under_price": price, "agree": agree, "sim_pmf": sim or pois(5), "base_pmf": base or pois(5)}


def synth(n_players=150, n_games=80, per_game=60, eff_agree=0.0, eff_dis=0.0, seed=1, p_lo=0.5, p_hi=0.7, sim_good=True, agree_by_level=None, level_miscal=None):
    rng = np.random.default_rng(seed)
    means = rng.uniform(2, 12, n_players)
    rows = []
    for g in range(n_games):
        for k in range(per_game):
            pi = int(rng.integers(0, n_players))
            market = MARKETS[int(rng.integers(0, len(MARKETS)))]
            p = float(rng.uniform(p_lo, p_hi))
            if level_miscal is not None:
                p = float(rng.choice([0.52, 0.68]))
            agree = int(rng.random() < (0.6 if agree_by_level is None else (agree_by_level[0] if p < 0.6 else agree_by_level[1])))
            shift = (eff_agree if agree else eff_dis) + (0 if level_miscal is None else (level_miscal if p > 0.6 else -level_miscal))
            hit = int(rng.random() < min(max(p + shift, 0.01), 0.99))
            side = "over" if rng.random() < 0.5 else "under"
            actual = int(rng.poisson(means[pi]))
            sim = pois(means[pi]) if sim_good else pois(float(means.mean()))
            rows.append(row(pid=f"p{pi}", gid=f"g{g}", market=market, p_fav=p, hit=hit, agree=agree, side=side, actual=actual, sim=sim, base=pois(float(means.mean()))))
    return rows


# ---- hand-computed fixtures (no randomness)

def test_discrete_crps_by_hand():
    assert crps_pmf([0.5, 0.5], 1) == pytest.approx(0.25)
    assert crps_pmf([0.0, 1.0], 0) == pytest.approx(1.0)
    assert crps_pmf([0.0, 0.0, 1.0], 2) == pytest.approx(0.0)                 # a point mass on the actual
    assert crps_pmf([1.0], 3) == pytest.approx(3.0)                             # mass at 0, actual 3: F=1 on x=0..2 against 0
    # F = [.25,.75,1]; y = 1 -> (.25-0)^2 + (.75-1)^2 + (1-1)^2 = .0625 + .0625
    assert crps_pmf([0.25, 0.5, 0.25], 1) == pytest.approx(0.125)


def test_residual_gap_by_hand():
    rows = [row("a", "g1", p_fav=0.6, hit=1, agree=1), row("b", "g2", p_fav=0.6, hit=0, agree=1),
            row("c", "g3", p_fav=0.55, hit=0, agree=0), row("d", "g4", p_fav=0.55, hit=0, agree=0)]
    d = Data(rows)
    # agree residuals: +0.4, -0.6 (mean -0.1); disagree: -0.55, -0.55 (mean -0.55): gap 0.45 -> 45 points
    assert gap(np.ones(d.nB), d.agree, d.resid) == pytest.approx(45.0)
    assert math.isnan(gap(np.array([1.0, 1.0, 0.0, 0.0]), d.agree, d.resid))      # an arm with zero weight is not estimable


def test_pooling_order_and_insufficient_overlap_by_hand():
    a = np.array
    # bin0 ok; bin1 deficient -> carried into bin2 (18/18 ok); bin3 deficient -> carried into bin4 (24/13 ok): three strata
    st = pool_market(a([12, 3, 15, 4, 20.]), a([11, 2, 16, 3, 10.]), a([1, 1, 1, 1, 1.]), a([1, 1, 1, 1, 1.]))
    assert [(x[0], x[1]) for x in st] == [(12, 11), (18, 18), (24, 13)]
    # a deficient remainder at the top is merged into the stratum below it
    st = pool_market(a([12, 3, 0, 0, 0.]), a([11, 2, 0, 0, 0.]), a([0] * 5), a([0] * 5))
    assert [(x[0], x[1]) for x in st] == [(15, 13)]
    # never reaching MIN_ARM in both arms: the market is dropped (insufficient overlap)
    assert pool_market(a([3, 2, 0, 0, 0.]), a([20, 20, 0, 0, 0.]), a([0] * 5), a([0] * 5)) == []
    assert pool_market(a([0.] * 5), a([0.] * 5), a([0.] * 5), a([0.] * 5)) == []


def s1_fixture(extra=()):
    spec = [(0, 0.52, 20, 14, 20, 10), (0, 0.57, 10, 6, 10, 4), (1, 0.52, 10, 6, 10, 4), (1, 0.57, 10, 5, 10, 5), (2, 0.52, 10, 7, 10, 3), (2, 0.57, 10, 5, 10, 5)]
    rows, n = [], 0
    for m, p, na, ha, nd, hd in list(spec) + list(extra):
        for arm, cnt, hits in ((1, na, ha), (0, nd, hd)):
            for i in range(cnt):
                rows.append(row(f"p{n}", f"g{n % 7}", market=MARKETS[m], p_fav=p, hit=int(i < hits), agree=arm))
                n += 1
    return rows


def test_s1_equals_the_size_weighted_stratum_gaps_by_hand():
    d = Data(s1_fixture())
    det = s1_stat(d, np.ones(d.nB), detail=True)
    # gaps 20, 20, 20, 0, 40, 0 with sizes 40, 20, 20, 20, 20, 20 -> 2400 / 140
    assert det["estimable"] and det["value"] == pytest.approx(2400 / 140) and det["strata"] == 6 and det["share_retained"] == pytest.approx(1.0)


def test_s1_not_estimable_with_too_few_markets_or_groups_and_reports_dropped_markets():
    rows = [r for r in s1_fixture() if r["market"] in ("pts", "reb")]            # two markets only
    d = Data(rows)
    det = s1_stat(d, np.ones(d.nB), detail=True)
    assert not det["estimable"] and math.isnan(det["value"]) and det["markets_kept"] == ["pts", "reb"]
    thin = [row(f"t{i}", "gt", market="blk", p_fav=0.52, hit=1, agree=1 if i < 2 else 0) for i in range(8)]          # 2 vs 6: below MIN_ARM in both arms
    d = Data(s1_fixture() + thin)
    det = s1_stat(d, np.ones(d.nB), detail=True)
    assert det["estimable"] and "blk" in det["markets_dropped"] and det["share_retained"] < 1.0


def test_pushes_and_voids_never_enter_track_b_but_pushes_count_for_track_a():
    rows = [row("a", "g1"), row("b", "g2", status="push", actual=5), row("c", "g3", status="void_dnp", actual=None), row("d", "g4", status="unresolved", actual=None)]
    d = Data(rows)
    assert d.nB == 1 and len(d.a_cs) == 2                                         # settled + push have an actual for CRPS; voids/unresolved have none


# ---- decision categories (mutually exclusive, ordered)

IV = lambda lo, hi, stable=True: {"lo": lo, "hi": hi, "discard": 0.0 if stable else 0.2, "stable": stable}


@pytest.mark.parametrize("T,iv,ivs,invalid,expect", [
    (0.05, IV(0.01, 0.09), [IV(0.0, 0.1)] * 3, True, "invalid"),
    (-0.03, IV(-0.05, -0.01), [IV(-0.05, 0.0)] * 3, False, "harmful"),
    (0.03, IV(0.01, 0.05), [IV(0.0, 0.1)] * 3, False, "pass"),
    (0.03, IV(0.01, 0.05), [IV(0.0, 0.1), IV(-0.1, -0.01), IV(0.0, 0.1)], False, "positive_below_gate_or_mixed"),
    (0.015, IV(0.005, 0.025), [IV(0.0, 0.1)] * 3, False, "positive_below_gate_or_mixed"),
    (0.0, IV(-0.01, 0.01), [IV(-0.02, 0.02)] * 3, False, "worthwhile_improvement_ruled_out"),
    (0.01, IV(-0.01, 0.04), [IV(-0.02, 0.05)] * 3, False, "inconclusive"),
    (0.03, IV(0.01, 0.05, stable=False), [IV(0.0, 0.1)] * 3, False, "inconclusive"),
])
def test_track_a_categories(T, iv, ivs, invalid, expect):
    assert decide_track_a(T, iv, ivs, invalid)["category"] == expect


@pytest.mark.parametrize("h1,iv,s1iv,s1ok,invalid,expect", [
    (5.0, IV(1, 9), IV(1, 9), True, True, "invalid"),
    (-4.0, IV(-8, -1), IV(-8, 0), True, False, "reversed"),
    (4.0, IV(1, 7), IV(0.5, 7), True, False, "separation_established"),
    (4.0, IV(1, 7), IV(-0.5, 7), True, False, "separation_not_robust_to_market_mix"),
    (4.0, IV(1, 7), IV(None, None, False), False, False, "separation_not_robust_to_market_mix"),
    (2.0, IV(0.5, 3.5), IV(0.2, 3), True, False, "positive_below_target"),
    (0.5, IV(-1.0, 2.0), IV(-1, 2), True, False, "worthwhile_effect_ruled_out"),
    (1.5, IV(-1.0, 4.0), IV(-1, 4), True, False, "inconclusive"),
    (4.0, IV(1, 7, stable=False), IV(1, 7), True, False, "inconclusive"),
])
def test_track_b_categories(h1, iv, s1iv, s1ok, invalid, expect):
    assert decide_track_b(h1, iv, s1iv, s1ok, invalid)["category"] == expect


def test_interval_discards_and_flags_instability():
    x = np.array([1.0] * 90 + [float("nan")] * 10)
    iv = interval(x)
    assert iv["discard"] == pytest.approx(0.1) and iv["stable"] is False
    assert interval(np.array([float("nan")] * 5))["lo"] is None


# ---- full analysis on synthetic data with known effects

REPS = 300


def test_track_a_detects_a_better_simulator_and_not_an_identical_one():
    better = run_primary(synth(sim_good=True, seed=3), reps=REPS)["track_a"]
    assert better["T"] > 0.02 and better["category"] == "pass" and better["T_interval"]["lo"] > 0
    rows = synth(sim_good=True, seed=3)
    for r in rows:
        r["sim_pmf"] = r["base_pmf"]
    same = run_primary(rows, reps=REPS)["track_a"]
    assert same["T"] == pytest.approx(0.0, abs=1e-9) and same["category"] in ("inconclusive", "worthwhile_improvement_ruled_out")
    worse = synth(sim_good=True, seed=3)
    for r in worse:
        r["sim_pmf"], r["base_pmf"] = r["base_pmf"], r["sim_pmf"]
    assert run_primary(worse, reps=REPS)["track_a"]["category"] == "harmful"


def test_track_b_separation_established_then_h2_evaluated():
    res = run_primary(synth(eff_agree=0.06, eff_dis=-0.06, seed=4), reps=REPS)["track_b"]
    assert res["category"] == "separation_established" and 9 < res["H1"] < 15 and res["H1_interval"]["lo"] > 0 and res["S1"]["estimable"]
    assert res["H2"]["evaluated"] and res["H2"]["passes"] is True
    assert res["S2_converged"] and res["S2_ame"] > 5


def test_track_b_null_and_reversed_effects():
    null = run_primary(synth(seed=5), reps=REPS)["track_b"]
    assert null["category"] in ("inconclusive", "worthwhile_effect_ruled_out") and not null["H2"]["evaluated"]
    rev = run_primary(synth(eff_agree=-0.07, eff_dis=0.07, seed=6), reps=REPS)["track_b"]
    assert rev["category"] == "reversed" and rev["H1"] < -9


def test_a_gap_that_comes_only_from_market_mix_is_not_robust():
    # favorites at high probability beat their price and low-probability favorites miss it, in both arms; agree rows sit mostly at high probability
    rows = synth(n_games=120, per_game=80, level_miscal=0.06, agree_by_level=(0.2, 0.8), seed=7)
    res = run_primary(rows, reps=REPS)["track_b"]
    assert res["H1"] > P.TARGET_B and res["H1_interval"]["lo"] > 0                 # the raw gap looks like separation
    assert abs(res["S1"]["value"]) < 2.0                                           # but within strata there is none
    assert res["category"] == "separation_not_robust_to_market_mix" and not res["H2"]["evaluated"]


def test_a_sparse_arm_makes_the_interval_unstable_instead_of_guessing():
    rows = synth(n_players=150, n_games=60, per_game=40, eff_agree=0.05, eff_dis=-0.05, seed=8)
    keep = [r for r in rows if r["agree"] == 1]
    dis = [r for r in rows if r["agree"] == 0][:4]
    for i, r in enumerate(dis):
        r["player_id"], r["game_id"] = f"p{i % 2}", f"g{i % 2}"                    # the whole disagree arm: two players, two games
    res = run_primary(keep + dis, reps=REPS)["track_b"]
    assert res["H1_interval"]["discard"] > P.DISCARD_MAX and res["H1_interval"]["stable"] is False
    assert res["category"] == "inconclusive"


def test_same_rows_and_seed_reproduce_the_result_exactly_and_the_seed_matters():
    rows = synth(n_games=40, per_game=40, eff_agree=0.04, eff_dis=-0.04, seed=9)
    a, b = run_primary(rows, reps=200), run_primary(rows, reps=200)
    assert a == b
    c = run_primary(rows, reps=200, seed=10)
    assert c["track_b"]["H1"] == a["track_b"]["H1"] and c["track_b"]["H1_interval"] != a["track_b"]["H1_interval"]       # the estimate is fixed, the interval moves with the seed
    assert a["protocol"] == P.PROTOCOL_VERSION and a["seed"] == P.BOOT_SEED


def test_row_order_does_not_change_the_result():
    rows = synth(n_games=30, per_game=40, eff_agree=0.04, eff_dis=-0.04, seed=11)
    shuffled = list(reversed(rows))
    assert run_primary(rows, reps=150) == run_primary(shuffled, reps=150)


def test_tiny_or_invalid_inputs_are_labelled_not_analysed():
    tiny = run_primary([row("a", "g1")], reps=50)
    assert tiny["track_a"]["category"] == "inconclusive" and tiny["track_b"]["category"] == "inconclusive"
    inv = run_primary(synth(n_games=20, per_game=30, seed=12), coverage_ok=False, reps=100)
    assert inv["track_a"]["category"] == "invalid" and inv["track_b"]["category"] == "invalid"
    assert run_primary(synth(n_games=20, per_game=30, seed=12), integrity_ok=False, reps=100)["track_b"]["category"] == "invalid"


def test_descriptive_secondary_outputs_are_present_and_do_not_drive_the_category():
    res = run_primary(synth(eff_agree=0.05, eff_dis=-0.05, seed=13), reps=100)["track_b"]
    g = res["descriptive"]["groups"]
    assert g["agree"]["n"] > g["disagree"]["n"] > 0 and g["agree"]["hit"] > g["agree"]["devig"]
    assert g["agree"]["break_even"] == pytest.approx(0.5238, abs=1e-3) and set(res["descriptive"]["by_market"]) == set(MARKETS)
    assert res["sensitivity"]["player_only"]["lo"] is not None and res["sensitivity"]["game_only"]["lo"] is not None
