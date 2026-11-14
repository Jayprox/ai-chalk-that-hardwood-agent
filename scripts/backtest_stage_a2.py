"""Stage A2: which inputs add skill beyond the market's own price?

Read-only against Hardwood (about 300 calls, no model calls, no cost). Run from the project folder with the venv active:

    python -m scripts.backtest_stage_a2 --date 2026-11-13

Question: the closing line (and its prices) already hold the market's view. Does the minutes-times-rate simulation, the opponent's
matchup rank, rest / home / starter status, or a player's hit-rate history add anything on top of it?

Method
  1. Same player set, game logs and simulation as Stage A (scripts.backtest_stage_a).
  2. Past prop boards for every game date supply, AS OF THAT DAY: the over and under prices (devigged into a market probability),
     the opponent's matchup rank, and the player's over-under record at that line before the game.
  3. Several logistic models are compared, each adding a group of inputs to a calibrated version of the market probability.
  4. Honest validation: forward chaining by date (train on earlier dates, test on the next block), never on the data a model
     was fitted on. Differences are shown with a 95% interval from a cluster bootstrap (one cluster = one player-game), because
     one player-game appears in several markets and the plain sample size overstates the evidence.

Reading the result: a model "adds skill" only if its interval for the Brier difference against the calibrated market excludes zero.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from scripts.backtest_stage_a import Params, gather, run_backtest

MARKETS = ["pts", "reb", "ast", "fg3m", "pa", "pr", "ra", "pra", "stl", "blk"]

GROUPS = {
    "sim": ["z_sim"],
    "matchup": ["mu_rank", "mu_vs_avg"],
    "context": ["home", "rest0", "rest_long", "started", "min_trend"],
    "history": ["hr_prior", "b_last10"],
}

MODELS: List[Tuple[str, List[str]]] = [
    ("market, calibrated", ["logit_mkt"]),
    ("sim alone, recalibrated", ["logit_sim"]),
    ("market + sim", ["logit_mkt"] + GROUPS["sim"]),
    ("market + sim + matchup", ["logit_mkt"] + GROUPS["sim"] + GROUPS["matchup"]),
    ("market + sim + matchup + context", ["logit_mkt"] + GROUPS["sim"] + GROUPS["matchup"] + GROUPS["context"]),
    ("market + everything", ["logit_mkt"] + GROUPS["sim"] + GROUPS["matchup"] + GROUPS["context"] + GROUPS["history"]),
]


# --------------------------------------------------------------------------- prices

def american_to_prob(x: float) -> float:
    return 100.0 / (100.0 + x) if x > 0 else abs(x) / (abs(x) + 100.0)


def devig(over_price: Optional[float], under_price: Optional[float]) -> Optional[float]:
    """Market probability of the over with the bookmaker's margin removed. None if a price is missing."""
    if over_price is None or under_price is None:
        return None
    try:
        o, u = american_to_prob(float(over_price)), american_to_prob(float(under_price))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return o / (o + u) if (o + u) > 0 else None


def logit(p, eps: float = 1e-3):
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    return np.log(p / (1 - p))


# --------------------------------------------------------------------------- board join

async def fetch_boards(hw, dates: List[str]) -> Tuple[Dict[tuple, dict], dict]:
    sem = asyncio.Semaphore(5)
    info: Dict[str, Any] = {"calls": 0, "errors": []}

    async def one(date: str, market: str):
        async with sem:
            return date, market, await hw.get("/props", {"market": market, "date": date})

    results = await asyncio.gather(*(one(d, m) for d in dates for m in MARKETS), return_exceptions=True)
    boards: Dict[tuple, dict] = {}
    for res in results:
        info["calls"] += 1
        if isinstance(res, BaseException):
            info["errors"].append(type(res).__name__)
            continue
        date, market, body = res
        for r in body.get("data") or []:
            if r.get("player_id") and r.get("game_id"):
                boards[(str(r["player_id"]), str(r["game_id"]), market)] = r
    info["board_rows"] = len(boards)
    return boards, info


def attach_board(records: List[dict], boards: Dict[tuple, dict]) -> Tuple[List[dict], dict]:
    rows: List[dict] = []
    stats = {"records": len(records), "no_board_row": 0, "no_prices": 0, "no_matchup": 0, "venue_values": Counter()}
    for rec in records:
        b = boards.get((str(rec.get("player_id")), str(rec.get("game_id")), rec["market"]))
        if b is None:
            stats["no_board_row"] += 1
            continue
        p_mkt = devig(b.get("over_price"), b.get("under_price"))
        if p_mkt is None:
            stats["no_prices"] += 1
            continue
        mu = b.get("matchup") or {}
        if mu.get("rank") is None:
            stats["no_matchup"] += 1
        l10 = b.get("last10") or {}
        over, under = (l10.get("over") or 0), (l10.get("under") or 0)
        stats["venue_values"][str(rec.get("venue"))] += 1
        rows.append({**rec, "p_mkt": p_mkt, "mu_rank_raw": mu.get("rank"), "mu_of": mu.get("of") or 30, "mu_vs_avg_raw": mu.get("vs_avg"),
                     "b_over": over, "b_under": under})
    stats["kept"] = len(rows)
    stats["venue_values"] = dict(stats["venue_values"])
    return rows, stats


def build_features(rows: List[dict]) -> Dict[str, np.ndarray]:
    n = len(rows)
    col = lambda f: np.array([f(r) for r in rows], dtype=float)
    sd = np.maximum(col(lambda r: r["sim_sd"]), 0.5)
    f = {
        "y": col(lambda r: r["over"]),
        "p_mkt": col(lambda r: r["p_mkt"]),
        "p_sim": col(lambda r: r["p_sim"]),
        "logit_mkt": logit(col(lambda r: r["p_mkt"])),
        "logit_sim": logit(col(lambda r: r["p_sim"])),
        "z_sim": np.clip((col(lambda r: r["sim_mean"]) - col(lambda r: r["line"])) / sd, -4, 4),
        "mu_rank": col(lambda r: 0.0 if r["mu_rank_raw"] is None else (r["mu_rank_raw"] - (r["mu_of"] + 1) / 2) / ((r["mu_of"] - 1) / 2)),
        "mu_vs_avg": col(lambda r: 0.0 if r["mu_vs_avg_raw"] is None else r["mu_vs_avg_raw"]),
        "home": col(lambda r: 1.0 if str(r.get("venue")).lower() in ("home", "h") else 0.0),
        "rest0": col(lambda r: 1.0 if r.get("rest_days") == 0 else 0.0),
        "rest_long": col(lambda r: 1.0 if (r.get("rest_days") or 0) >= 3 else 0.0),
        "started": col(lambda r: 1.0 if r.get("started") else 0.0),
        "min_trend": np.clip(col(lambda r: r["min_trend"]) / 5.0, -3, 3),
        "hr_prior": logit(col(lambda r: r["p_hitrate"])),
        "b_last10": logit(col(lambda r: (r["b_over"] + 1) / (r["b_over"] + r["b_under"] + 2))),
        # Stage B inputs (zero when a row carries none)
        "absent_min": np.clip(col(lambda r: r.get("absent_min", 0.0)) / 30.0, 0, 3),
        "absent_starters": np.clip(col(lambda r: r.get("absent_starters", 0)), 0, 3),
    }
    dates = np.array([r["date"] for r in rows])
    f["date"] = dates
    keys = [(r["player_id"], r["date"]) for r in rows]
    uniq = {k: i for i, k in enumerate(dict.fromkeys(keys))}
    f["cluster"] = np.array([uniq[k] for k in keys])
    assert all(len(v) == n for v in f.values())
    return f


# --------------------------------------------------------------------------- models

def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 5.0, iters: int = 50) -> Tuple[np.ndarray, np.ndarray]:
    """Ridge logistic regression (intercept not penalised). Returns weights and the final Hessian."""
    n, d = X.shape
    Xb = np.c_[np.ones(n), X]
    R = l2 * np.eye(d + 1)
    R[0, 0] = 0.0
    w = np.zeros(d + 1)
    H = np.eye(d + 1)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(Xb @ w)))
        g = Xb.T @ (p - y) + R @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + R + 1e-8 * np.eye(d + 1)
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    return w, H


def standardize(train: np.ndarray, test: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    mu = train.mean(axis=0)
    sd = train.std(axis=0)
    sd[sd < 1e-9] = 1.0
    return (train - mu) / sd, (test - mu) / sd, mu, sd


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-(np.c_[np.ones(len(X)), X] @ w)))


def forward_chain_masks(dates: np.ndarray, blocks: int = 4) -> List[Tuple[np.ndarray, np.ndarray]]:
    """Train on the first k date-blocks, test on block k (k = 1 .. blocks-1). Never trains on a date at or after the test dates."""
    ud = sorted(set(dates.tolist()))
    chunks = [set(c.tolist()) for c in np.array_split(np.array(ud), blocks)]
    out = []
    for k in range(1, blocks):
        train_dates = set().union(*chunks[:k])
        out.append((np.isin(dates, list(train_dates)), np.isin(dates, list(chunks[k]))))
    return out


def oos_predictions(feats: Dict[str, np.ndarray], cols: List[str], l2: float = 5.0, blocks: int = 4) -> Tuple[np.ndarray, np.ndarray]:
    """Out-of-sample probabilities for every test row across the forward-chained blocks (rows in training-only block are NaN)."""
    n = len(feats["y"])
    pred = np.full(n, np.nan)
    X = np.c_[tuple(feats[c] for c in cols)]
    for tr, te in forward_chain_masks(feats["date"], blocks):
        Xtr, Xte, _, _ = standardize(X[tr], X[te])
        w, _ = fit_logit(Xtr, feats["y"][tr], l2)
        pred[te] = predict(w, Xte)
    return pred, ~np.isnan(pred)


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def auc(p: np.ndarray, y: np.ndarray) -> float:
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p))
    ranks[order] = np.arange(1, len(p) + 1)
    # average ranks for ties
    _, inv, counts = np.unique(p, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    ranks = (sums / counts)[inv]
    n1 = y.sum()
    n0 = len(y) - n1
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def cluster_bootstrap_diff(e_model: np.ndarray, e_base: np.ndarray, cluster: np.ndarray, reps: int = 1000, seed: int = 11) -> Tuple[float, float, float]:
    """Mean of (model squared error - base squared error) with a 95% interval, resampling whole clusters. Negative = model better."""
    d = e_model - e_base
    _, inv = np.unique(cluster, return_inverse=True)
    k = inv.max() + 1
    sums = np.bincount(inv, weights=d, minlength=k)
    cnt = np.bincount(inv, minlength=k).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(reps, k))
    means = sums[idx].sum(axis=1) / cnt[idx].sum(axis=1)
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def analyze(feats: Dict[str, np.ndarray], l2: float = 5.0, blocks: int = 4, reps: int = 1000, models: Optional[list] = None) -> dict:
    models = models or MODELS
    preds = {name: oos_predictions(feats, cols, l2, blocks) for name, cols in models}
    test = np.logical_and.reduce([m for _p, m in preds.values()])
    y = feats["y"][test]
    cl = feats["cluster"][test]
    base_name = models[0][0]
    base_err = (preds[base_name][0][test] - y) ** 2
    table = []
    const = np.full(test.sum(), 0.5)
    refs = {"50% always": const, "market, raw (devigged prices)": feats["p_mkt"][test], "sim, raw": feats["p_sim"][test]}
    for name, p in refs.items():
        e = (p - y) ** 2
        diff, lo, hi = cluster_bootstrap_diff(e, base_err, cl, reps)
        table.append({"model": name, "brier": brier(p, y), "logloss": logloss(p, y), "auc": auc(p, y), "diff": diff, "lo": lo, "hi": hi})
    for name, _cols in models:
        p = preds[name][0][test]
        diff, lo, hi = cluster_bootstrap_diff((p - y) ** 2, base_err, cl, reps)
        table.append({"model": name, "brier": brier(p, y), "logloss": logloss(p, y), "auc": auc(p, y), "diff": diff, "lo": lo, "hi": hi})
    best = min(models[1:], key=lambda m: brier(preds[m[0]][0][test], y))[0]
    bins = [(0, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 1.01)]
    pb = preds[best][0][test]
    lift = []
    for lo_, hi_ in bins:
        sel = (pb >= lo_) & (pb < hi_)
        lift.append({"bin": f"{lo_:.2f}-{min(hi_, 1):.2f}", "n": int(sel.sum()),
                     "mean_predicted": float(pb[sel].mean()) if sel.any() else None,
                     "actual_over_rate": float(y[sel].mean()) if sel.any() else None})
    # coefficients of the fullest model, fitted on ALL rows (interpretation only, not a validation)
    full_cols = models[-1][1]
    X = np.c_[tuple(feats[c] for c in full_cols)]
    Xs, _, _, _ = standardize(X, X)
    w, H = fit_logit(Xs, feats["y"], l2)
    se = np.sqrt(np.diag(np.linalg.inv(H)))
    coefs = [{"feature": "intercept", "coef": float(w[0]), "z": float(w[0] / se[0])}] + \
            [{"feature": c, "coef": float(w[i + 1]), "z": float(w[i + 1] / se[i + 1])} for i, c in enumerate(full_cols)]
    return {"n_test": int(test.sum()), "n_all": int(len(feats["y"])), "n_clusters_test": int(len(np.unique(cl))),
            "table": table, "best_model": best, "lift_best": lift, "coefs_full_fit": coefs,
            "test_dates": sorted(set(feats["date"][test].tolist()))}


# --------------------------------------------------------------------------- report

def verdict(row: dict) -> str:
    if row["lo"] > 0:
        return "WORSE"
    if row["hi"] < 0:
        return "better"
    return "no clear difference"


def format_report(info: dict, join: dict, res: dict) -> str:
    L: List[str] = []
    L.append(f"Players {info.get('players_loaded')}, games {info.get('games_loaded')}. Past-board rows loaded: {join.get('board_rows')}.")
    L.append(f"Records {join['records']}: kept {join['kept']} (no board row {join['no_board_row']}, no prices {join['no_prices']}; "
             f"{join['no_matchup']} kept rows had no matchup yet). Venue values seen: {join['venue_values']}")
    L.append(f"Out-of-sample rows: {res['n_test']} of {res['n_all']} in {res['n_clusters_test']} player-games, dates {res['test_dates'][0]} to {res['test_dates'][-1]}")
    L.append("(forward chaining: the first quarter of dates is only ever used to train)")
    L.append("")
    L.append("MODELS, tested on dates they were not trained on. Diff = Brier minus the calibrated market's Brier (negative is better).")
    L.append(f"  {'model':36} {'Brier':>7} {'logloss':>8} {'AUC':>6} {'diff':>8}  {'95% interval':>18}  verdict")
    for r in res["table"]:
        L.append(f"  {r['model']:36} {r['brier']:>7.4f} {r['logloss']:>8.4f} {r['auc']:>6.3f} {r['diff']:>+8.4f}  [{r['lo']:>+7.4f},{r['hi']:>+7.4f}]  {verdict(r)}")
    L.append("")
    L.append(f"BEST MODEL by Brier: {res['best_model']}. Predicted vs actual over rate, out of sample:")
    L.append(f"  {'bin':10} {'n':>6} {'predicted':>10} {'actual':>8}")
    for r in res["lift_best"]:
        if r["n"]:
            L.append(f"  {r['bin']:10} {r['n']:>6} {100 * r['mean_predicted']:>9.1f}% {100 * r['actual_over_rate']:>7.1f}%")
        else:
            L.append(f"  {r['bin']:10} {0:>6}")
    L.append("")
    L.append("COEFFICIENTS of the fullest model, fitted on all rows (standardised inputs; |z| over 2 is stronger evidence, but z is overstated")
    L.append("because one player-game appears in several markets; this is for interpretation, not validation)")
    for c in res["coefs_full_fit"]:
        L.append(f"  {c['feature']:12} coef {c['coef']:>+7.3f}  z {c['z']:>+6.1f}")
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
    params = Params(draws=args.draws)
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    t0 = time.time()
    try:
        print(f"Date {date}, season {season}. Loading boards and game logs...")
        players, info = await gather(hw, date, season, args.max_players)
        dates = sorted({g["date"] for _n, logs in players.values() for g in logs if g.get("date")})
        print(f"Loaded {info['players_loaded']} players in {time.time() - t0:.0f}s. Loading {len(dates)} past boards x {len(MARKETS)} markets...")
        boards, binfo = await fetch_boards(hw, dates)
        if binfo["errors"]:
            print(f"Board load errors: {len(binfo['errors'])} ({Counter(binfo['errors']).most_common(2)})")
    finally:
        await hw.aclose()
    print("Simulating and fitting...")
    records = run_backtest(players, params)
    rows, join = attach_board(records, boards)
    join["board_rows"] = binfo["board_rows"]
    if len(rows) < 400:
        print(f"Only {len(rows)} usable rows after joining boards; not enough to test. Join stats: {join}")
        return 1
    feats = build_features(rows)
    res = analyze(feats, l2=args.l2, reps=args.reps)
    print()
    print(format_report(info, join, res))
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest-stage-a2-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"date": date, "season": season, "params": params.__dict__, "l2": args.l2, "info": info, "join": join,
                                "result": res}, indent=1, default=str))
    print(f"\nSaved to {path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="US Eastern date whose boards pick the players (default today)")
    ap.add_argument("--draws", type=int, default=4000)
    ap.add_argument("--l2", type=float, default=5.0, help="ridge strength for the logistic models")
    ap.add_argument("--reps", type=int, default=1000, help="bootstrap resamples")
    ap.add_argument("--max-players", type=int, default=None, help="cap for a quick trial run")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
