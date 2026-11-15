"""Can the raw simulation probability be made honest?

Read-only against Hardwood (no model calls, no cost). Run from the project folder:

    python -m scripts.backtest_calibrate --date 2026-11-14

Fits p_cal = sigmoid(a + b * logit(p_raw)) and scores it only on dates AFTER the ones it was fitted on (forward chaining), against
the raw probability and a flat 50%. Two forms: slope-only (a = 0, direction never changes) and slope plus intercept. Brier
differences carry a 95% interval from a cluster bootstrap (one cluster = one player-game). The fitted slope per fold shows whether
the shrinkage is stable.

Reading it: recalibration is worth shipping only if it beats the raw probability clearly. Beating a flat 50% is a separate, harder
bar and is reported but not required, since the dashboard uses this number for display, not for the strong rule.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.calibration import EPS
from scripts.backtest_stage_a import Params, gather, run_backtest
from scripts.backtest_stage_a2 import cluster_bootstrap_diff


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def fit(p: np.ndarray, y: np.ndarray, intercept: bool, l2: float = 1e-3, iters: int = 60) -> Tuple[float, float]:
    """Maximum-likelihood (a, b) for y ~ sigmoid(a + b * logit(p)); a fixed at 0 when intercept is False."""
    x = _logit(p)
    a, b = 0.0, 1.0
    for _ in range(iters):
        q = 1 / (1 + np.exp(-(a + b * x)))
        w = q * (1 - q) + 1e-9
        if intercept:
            g = np.array([(y - q).sum(), ((y - q) * x).sum() - l2 * (b - 1)])
            H = np.array([[w.sum(), (w * x).sum()], [(w * x).sum(), (w * x * x).sum() + l2]])
            step = np.linalg.solve(H + np.eye(2) * 1e-9, g)
            a, b = a + step[0], b + step[1]
        else:
            g = ((y - q) * x).sum() - l2 * (b - 1)
            b += g / ((w * x * x).sum() + l2)
    return float(a), float(b)


def predict(p: np.ndarray, a: float, b: float) -> np.ndarray:
    return 1 / (1 + np.exp(-(a + b * _logit(p))))


def forward_chain(records: List[dict], blocks: int = 4, reps: int = 1000) -> dict:
    dates = sorted({r["date"] for r in records})
    edges = np.array_split(np.array(dates), blocks)
    block_of = {d: i for i, grp in enumerate(edges) for d in grp}
    blk = np.array([block_of[r["date"]] for r in records])
    p = np.array([r["p_sim"] for r in records], dtype=float)
    y = np.array([r["over"] for r in records], dtype=float)
    cl = np.array([f"{r['player_id']}|{r['game_id']}" for r in records])
    test = blk >= 1
    pred = {"raw": p.copy(), "slope_only": p.copy(), "slope_intercept": p.copy()}
    folds = []
    for k in range(1, blocks):
        tr, te = blk < k, blk == k
        if tr.sum() < 200 or te.sum() == 0:
            test &= blk != k
            continue
        _a0, b0 = fit(p[tr], y[tr], False)
        a1, b1 = fit(p[tr], y[tr], True)
        pred["slope_only"][te] = predict(p[te], 0.0, b0)
        pred["slope_intercept"][te] = predict(p[te], a1, b1)
        folds.append({"block": k, "train_n": int(tr.sum()), "test_n": int(te.sum()), "b_slope_only": b0, "a": a1, "b": b1})
    yt, clt = y[test], cl[test]
    err = {k: (v[test] - yt) ** 2 for k, v in pred.items()}
    err["fifty"] = (0.5 - yt) ** 2
    out = {"n_test": int(test.sum()), "folds": folds, "scores": {}}
    for k in ("raw", "slope_only", "slope_intercept"):
        out["scores"][k] = {"brier": float(err[k].mean())}
    out["scores"]["fifty"] = {"brier": float(err["fifty"].mean())}
    for k in ("slope_only", "slope_intercept"):
        for ref in ("raw", "fifty"):
            d, lo, hi = cluster_bootstrap_diff(err[k], err[ref], clt, reps)
            out["scores"][k][f"vs_{ref}"] = {"diff": d, "lo": lo, "hi": hi}
    # final fit on everything, the values that would ship
    a_all, b_all = fit(p, y, False)
    a2, b2 = fit(p, y, True)
    out["final"] = {"slope_only": {"a": 0.0, "b": b_all}, "slope_intercept": {"a": a2, "b": b2}, "n": int(len(y)), "dates": [dates[0], dates[-1]]}
    return out


def verdict(d: dict) -> str:
    return "better" if d["hi"] < 0 else "worse" if d["lo"] > 0 else "no clear difference"


def format_report(res: dict) -> str:
    L = [f"Forward-chained recalibration: scored on {res['n_test']:,} records from dates after the fitting data", ""]
    s = res["scores"]
    L.append(f"  raw sim            brier {s['raw']['brier']:.4f}")
    for k, name in (("slope_only", "slope only (a=0)"), ("slope_intercept", "slope + intercept")):
        v = s[k]
        L.append(f"  {name:18} brier {v['brier']:.4f}   vs raw {v['vs_raw']['diff']:+.4f} [{v['vs_raw']['lo']:+.4f}, {v['vs_raw']['hi']:+.4f}] {verdict(v['vs_raw'])};"
                 f"  vs 50% {v['vs_fifty']['diff']:+.4f} [{v['vs_fifty']['lo']:+.4f}, {v['vs_fifty']['hi']:+.4f}] {verdict(v['vs_fifty'])}")
    L.append(f"  flat 50%           brier {s['fifty']['brier']:.4f}")
    L.append("")
    L.append("Fitted per fold (training data only):")
    for f in res["folds"]:
        L.append(f"  fold {f['block']}: train {f['train_n']:,} test {f['test_n']:,}  slope-only b={f['b_slope_only']:.3f}   slope+intercept a={f['a']:+.3f} b={f['b']:.3f}")
    fin = res["final"]
    L.append("")
    L.append(f"Fit on all {fin['n']:,} records ({fin['dates'][0]} to {fin['dates'][1]}):  slope-only b={fin['slope_only']['b']:.3f};  slope+intercept a={fin['slope_intercept']['a']:+.3f} b={fin['slope_intercept']['b']:.3f}")
    return "\n".join(L)


async def main_async(args) -> int:
    from app.config import current_season, load_settings, now_et
    from app.hardwood import HardwoodClient

    s = load_settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    date = args.date or now_et().strftime("%Y-%m-%d")
    season = current_season(now_et())
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    t0 = time.time()
    try:
        print(f"Date {date}, season {season}. Loading boards and game logs...")
        players, info = await gather(hw, date, season, args.max_players)
    finally:
        await hw.aclose()
    print(f"Loaded in {time.time() - t0:.0f}s. Simulating...")
    records = run_backtest(players, Params(draws=args.draws, seed=args.seed))
    res = forward_chain(records, args.blocks)
    print()
    print(format_report(res))
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest-calibrate-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"date": date, "season": season, "info": info, "result": res}, indent=1, default=str))
    print(f"\nSaved to {path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date")
    ap.add_argument("--draws", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--max-players", type=int, default=None)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
