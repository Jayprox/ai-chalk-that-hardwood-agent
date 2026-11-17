import numpy as np
import pytest

from scripts.backtest_strong import STRONG_PCT, analyze, build_rows, diff_ci, group_stats, profit, wilson


def boards(n=300, date="2026-11-01"):
    rows = []
    for i in range(n):
        tilt = i / n                                    # a steady range of tilts, over side favored throughout
        rows.append({"date": date, "market": "pts", "player_id": f"p{i}", "game_id": f"g{i}", "line": 20.5,
                     "over_price": -(110 + int(250 * tilt)), "under_price": 100 + int(150 * tilt)})
    return rows


def records(rows, hit_rate_top=0.6, agree_rate=0.5, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i, b in enumerate(rows):
        top = i >= 0.9 * len(rows)
        over = 1 if rng.uniform() < (hit_rate_top if top else 0.52) else 0
        agrees = rng.uniform() < agree_rate
        out.append({"player_id": b["player_id"], "game_id": b["game_id"], "market": "pts", "over": over, "p_sim": 0.6 if agrees else 0.4, "line": b["line"]})
    return out


def test_wilson_and_profit_basics():
    lo, hi = wilson(60, 100)
    assert lo < 0.6 < hi and 0.50 < lo and hi < 0.70
    assert profit(-110, True) == pytest.approx(100 / 110) and profit(150, True) == 1.5 and profit(-110, False) == -1.0 and profit(None, True) is None


def test_percentile_is_taken_across_the_whole_board_and_unmatched_rows_are_dropped():
    b = boards()
    recs = records(b)[:-10]                              # ten props have no result (unfinished, DNP or push)
    rows = build_rows(b, recs)
    assert len(rows) == 290
    assert max(r["pct"] for r in rows) > 95 and all(r["side"] == "OVER" for r in rows)
    top = [r for r in rows if r["pct"] >= STRONG_PCT]
    assert 15 < len(top) < 31 and all(r["sim"] in ("agree", "disagree") for r in top)


def test_a_planted_edge_is_found_and_a_null_one_is_not():
    b = boards(n=2000)
    rows = build_rows(b, records(b, hit_rate_top=0.6, agree_rate=0.5, seed=1))
    res = analyze(rows)
    top = res["groups"]["top 10%"]
    assert top["hit"] > 0.55 and top["n"] > 150 and top["lo"] < top["hit"] < top["hi"]
    d = res["agree_vs_disagree"]
    assert d["lo"] <= d["diff"] <= d["hi"] and d["lo"] < 0 < d["hi"]            # sim agreement is unrelated to the hit in this synthetic world
    # plant an effect where the sim agreeing hits much more often
    rng = np.random.default_rng(2)
    recs = []
    for i, bb in enumerate(b):
        agrees = rng.uniform() < 0.5
        recs.append({"player_id": bb["player_id"], "game_id": bb["game_id"], "market": "pts", "p_sim": 0.6 if agrees else 0.4, "line": bb["line"],
                     "over": 1 if rng.uniform() < (0.75 if agrees else 0.35) else 0})
    res2 = analyze(build_rows(b, recs))
    assert res2["agree_vs_disagree"]["lo"] > 0


def test_diff_ci_handles_empty_groups_and_group_stats_empty():
    assert diff_ci([], [{"player_id": "a", "game_id": "b", "hit": True}]) is None
    assert group_stats([]) == {"n": 0}


def test_robustness_credits_only_what_the_price_does_not_explain():
    from scripts.backtest_strong import format_robustness, gap_stats, robustness
    b = boards(n=2000)
    rng = np.random.default_rng(3)
    recs = []
    for bb in b:
        agrees = rng.uniform() < 0.5
        recs.append({"player_id": bb["player_id"], "game_id": bb["game_id"], "market": "pts", "p_sim": 0.65 if agrees else 0.35, "prior_games": 9, "line": bb["line"],
                     "over": 1 if rng.uniform() < (0.8 if agrees else 0.3) else 0})
    rows = build_rows(b, recs)
    r = robustness(rows)
    d = r["overall"]["diff"]
    assert d["lo"] > 0 and r["overall"]["agree"]["gap"] > r["overall"]["disagree"]["gap"]
    assert set(r["by_market"]) == {"pts"} and r["by_prior_games"]["9-12 games"]["agree"]["n"] > 100
    lean = r["by_sim_lean"]
    assert lean["60%+"]["gap"] > lean["sim <40% on the favorite"]["gap"]
    assert "agree minus disagree" in format_robustness(r)
    assert gap_stats([]) == {"n": 0}


def test_rows_whose_result_was_scored_against_a_different_line_are_dropped_and_counted():
    b = boards(n=100)
    recs = records(b)
    recs[0]["line"] = 21.5                                       # the result was scored against another threshold
    recs[1]["line"] = None
    stats = {}
    rows = build_rows(b, recs, stats)
    assert stats["line_mismatch"] == 2 and stats["matched"] == 98 and len(rows) == 98
    assert all(r["rec_line"] == r["line"] for r in rows)


def test_game_and_date_clustered_intervals_are_reported_and_widen_with_shared_effects():
    from scripts.backtest_strong import robustness
    b = boards(n=1500)
    rows = build_rows(b, records(b, seed=4))
    for i, r in enumerate(rows):
        r["date"] = f"2026-11-{1 + i % 10:02d}"
    r = robustness(rows)["overall"]
    for k in ("diff", "diff_by_game", "diff_by_date"):
        assert r[k]["lo"] <= r[k]["diff"] <= r[k]["hi"]
