"""Fit the simulation's minutes model and per-stat spread on a full earlier season.

Read-only against Hardwood (a few hundred calls, no model calls, no cost). Run from the project folder:

    python -m scripts.fit_sim            # report only
    python -m scripts.fit_sim --write    # also write app/sim_fit.json, which the simulation loads at start-up

Step 1, minutes: ridge regression of the miss in a flat minutes average on facts from the player's own earlier games (starter rate,
minutes trend and volatility, blowout and foul tendency). Scored forward in time (fit on earlier dates, test on later ones) with a 95%
interval from a cluster bootstrap (one cluster = one player). Shipped only if the interval is clearly below zero.

Step 2, spread: with the minutes model in place, find the multiplier on each stat's dispersion that makes the simulation's central 80%
range hold 80% of outcomes (probability-integral-transform deciles as close to flat as possible). Fitted on the earlier half of the
dates and checked on the later half; a multiplier is shipped only if it also improves the held-out half.

This is stat accuracy, not market-beating. The result does not change the finding that the simulation adds no measured skill beyond the
betting price; it makes the simulation's own numbers more honest.
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

from app import minutes_model as mmod
from app.simulation import League, Params, is_played, league_params, simulate_player
from scripts.backtest_stage_a2 import cluster_bootstrap_diff
from scripts.habit_screen import gather_players

STATS = ("pts", "reb", "ast", "fg3m")
GRID = [1.0, 1.1, 1.2, 1.35, 1.5, 1.7, 2.0, 2.4, 3.0]


def minutes_rows(players: Dict[str, Tuple[str, List[dict]]], min_prior: int = 6) -> dict:
    X, y, base, dates, pid = [], [], [], [], []
    for p, (_n, logs) in players.items():
        played = sorted((g for g in logs if is_played(g)), key=lambda g: g.get("date") or "")
        for i in range(min_prior, len(played)):
            prior = played[:i]
            mean = float(np.mean([g["minutes"] for g in prior]))
            X.append(mmod.features(prior))
            y.append(played[i]["minutes"] - mean)
            base.append(mean)
            dates.append(played[i].get("date") or "")
            pid.append(p)
    return {"X": np.array(X), "y": np.array(y), "base": np.array(base), "dates": np.array(dates), "pid": np.array(pid)}


def minutes_forward(rows: dict, blocks: int = 4, lam: float = 50.0, reps: int = 1000) -> dict:
    ds = sorted(set(rows["dates"]))
    block_of = {d: i for i, grp in enumerate(np.array_split(np.array(ds), blocks)) for d in grp}
    blk = np.array([block_of[d] for d in rows["dates"]])
    pred = np.zeros(len(blk))
    for k in range(1, blocks):
        tr, te = blk < k, blk == k
        m = mmod.fit(rows["X"][tr], rows["y"][tr], lam)
        for i in np.where(te)[0]:
            pred[i] = float(np.clip(((rows["X"][i] - np.array(m.mean)) / (np.array(m.scale) + 1e-9)) @ np.array(m.coef) + m.intercept, -m.max_adjust, m.max_adjust))
    test = blk >= 1
    flat, model = rows["y"][test] ** 2, (rows["y"][test] - pred[test]) ** 2
    d, lo, hi = cluster_bootstrap_diff(model, flat, rows["pid"][test], reps)
    return {"n_test": int(test.sum()), "mse_flat": float(flat.mean()), "mse_model": float(model.mean()), "diff": d, "lo": lo, "hi": hi,
            "pct": 100 * d / float(flat.mean()), "helps": bool(hi < 0)}


def pit_table(players, lg: League, params: Params, mult: float, sample: int, seed: int = 3, draws: int = 600) -> dict:
    """PIT values by stat, per row, for one dispersion multiplier applied to every stat."""
    rng = np.random.default_rng(seed)
    out = {k: [] for k in STATS}
    crps = {k: [] for k in STATS}
    dates, pids = [], []
    items = []
    for p, (_n, logs) in players.items():
        played = sorted((g for g in logs if is_played(g)), key=lambda g: g.get("date") or "")
        for i in range(6, len(played)):
            items.append((p, played[:i], played[i]))
    step = max(1, len(items) // sample)
    for pid, prior, g in items[::step]:
        d = simulate_player(prior, lg, Params(draws=draws, minutes=params.minutes, use_minutes_model=params.use_minutes_model,
                                              disp_mult={k: mult for k in STATS}), rng)
        if d is None:
            continue
        dates.append(g.get("date") or "")
        pids.append(pid)
        for k in STATS:
            a = float(g.get(k) or 0)
            out[k].append(float((d[k] < a).mean() + rng.uniform() * (d[k] == a).mean()))
            crps[k].append(crps_from_draws(d[k], a))
    return {"dates": np.array(dates), "pids": np.array(pids), "pit": {k: np.array(v) for k, v in out.items()}, "crps": {k: np.array(v) for k, v in crps.items()}}


def crps_from_draws(draws: np.ndarray, actual: float) -> float:
    """Continuous ranked probability score of a sample against one outcome (lower is better; a proper score that rewards both
    sharpness and calibration, unlike the decile flatness used to choose the multiplier)."""
    x = np.sort(np.asarray(draws, dtype=float))
    n = len(x)
    pair = 2.0 * float(((2 * np.arange(1, n + 1) - n - 1) * x).sum()) / (n * n)      # E|X - X'|
    return float(np.abs(x - actual).mean() - 0.5 * pair)


def deciles(u: np.ndarray) -> np.ndarray:
    return np.array([((u >= a / 10) & (u < (a + 1) / 10)).mean() for a in range(10)])


def uniformity_loss(u: np.ndarray) -> float:
    return float(((deciles(u) - 0.1) ** 2).sum())


def central80(u: np.ndarray) -> float:
    return float(((u >= 0.1) & (u <= 0.9)).mean())


def fit_spread(players, lg: League, mm, sample: int = 5000, cut: Optional[str] = None, reps: int = 1000) -> dict:
    """Multipliers are chosen on dates before `cut` and judged on dates from `cut` on. The caller must make sure `mm` was fitted on
    dates before `cut` only, so nothing from the judged half reaches the minutes model. A multiplier ships only if, on the judged half,
    the central-80% coverage error and the decile flatness both improve and the CRPS improvement has a 95% interval below zero
    (resampling players)."""
    params = Params(minutes=mm, use_minutes_model=mm is not None)
    tables = {c: pit_table(players, lg, params, c, sample) for c in GRID}
    dates = sorted(set(tables[GRID[0]]["dates"]))
    cut = cut or dates[len(dates) // 2]
    res: dict = {"split_date": cut, "stats": {}, "grid": GRID}
    base = tables[1.0]
    late_mask = base["dates"] >= cut
    for k in STATS:
        early = {c: uniformity_loss(t["pit"][k][t["dates"] < cut]) for c, t in tables.items()}
        best = min(early, key=early.get)
        late = lambda c: tables[c]["pit"][k][tables[c]["dates"] >= cut]  # noqa: E731
        cov_b, cov_a = central80(late(1.0)), central80(late(best))
        d, lo, hi = cluster_bootstrap_diff(tables[best]["crps"][k][late_mask], base["crps"][k][late_mask], base["pids"][late_mask], reps)
        crps_base = float(base["crps"][k][late_mask].mean())
        res["stats"][k] = {
            "best_mult": best, "central80_late_before": cov_b, "central80_late_after": cov_a,
            "loss_late_before": uniformity_loss(late(1.0)), "loss_late_after": uniformity_loss(late(best)),
            "crps_late_before": crps_base, "crps_pct": 100 * d / crps_base if crps_base else 0.0, "crps_lo": lo, "crps_hi": hi,
        }
        res["stats"][k]["ship"] = bool(best > 1.0 and abs(cov_a - 0.8) < abs(cov_b - 0.8) and res["stats"][k]["loss_late_after"] < res["stats"][k]["loss_late_before"] and hi < 0)
    return res


def format_report(minutes: dict, spread: dict, mm, disp: Dict[str, float], players: int, games: int) -> str:
    L = [f"Fit on {players} players, {games:,} games", "",
         "STEP 1  Minutes model (scored on later dates only)",
         f"  squared error vs flat average: {minutes['mse_flat']:.2f} -> {minutes['mse_model']:.2f} ({minutes['pct']:+.1f}%, 95% interval [{minutes['lo']:+.2f}, {minutes['hi']:+.2f}], n={minutes['n_test']:,})",
         f"  verdict: {'helps, shipped' if minutes['helps'] else 'no clear gain, NOT shipped'}"]
    if mm is not None:
        L.append("  weights (minutes added per 1 standard deviation): " + ", ".join(f"{n} {w:+.2f}" for n, w in zip(mmod.FEATURES, mm.coef)) + f"; intercept {mm.intercept:+.2f}")
    L += ["", f"STEP 2  Spread (fitted before {spread['split_date']}, checked after it)"]
    for k, s in spread["stats"].items():
        L.append(f"  {k:5} multiplier {s['best_mult']:.2f}: central 80% range holds {100 * s['central80_late_before']:.0f}% -> {100 * s['central80_late_after']:.0f}% of later outcomes; "
                 f"decile flatness loss {s['loss_late_before']:.4f} -> {s['loss_late_after']:.4f}; CRPS {s['crps_pct']:+.2f}% [{100 * s['crps_lo'] / s['crps_late_before']:+.2f}, {100 * s['crps_hi'] / s['crps_late_before']:+.2f}]  {'shipped' if s['ship'] else 'not shipped'}")
    L.append("")
    L.append("Shipped multipliers: " + (", ".join(f"{k} x{v:.2f}" for k, v in disp.items()) or "none"))
    return "\n".join(L)


async def main_async(args) -> int:
    from datetime import timedelta
    from app.config import current_season, load_settings, now_et
    from app.hardwood import HardwoodClient, previous_season

    s = load_settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    today = now_et().date()
    dates = [(today - timedelta(days=k)).isoformat() for k in range(0, args.board_days, 3)]
    season = args.season or previous_season(current_season(now_et()))
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    t0 = time.time()
    try:
        print(f"Season {season}. Collecting players, then game logs...")
        players, info = await gather_players(hw, season, dates, args.max_players)
    finally:
        await hw.aclose()
    print(f"Loaded {info['players']} players, {info['games']:,} games in {time.time() - t0:.0f}s. Fitting...")
    lg = league_params(logs for _n, logs in players.values())
    rows = minutes_rows(players)
    minutes = minutes_forward(rows)
    mm = None
    mm_early = None
    cut = sorted(set(rows["dates"]))[len(set(rows["dates"])) // 2]
    if minutes["helps"]:
        mm = mmod.fit(rows["X"], rows["y"], fitted_on=f"{season}, {info['players']} players, {len(rows['y']):,} games")       # shipped
        before = rows["dates"] < cut
        mm_early = mmod.fit(rows["X"][before], rows["y"][before])                                                          # used only to judge the spread
    spread = fit_spread(players, lg, mm_early, args.sample, cut)
    disp = {k: s_["best_mult"] for k, s_ in spread["stats"].items() if s_["ship"]}
    print()
    print(format_report(minutes, spread, mm, disp, info["players"], info["games"]))
    meta = {"season": season, "players": info["players"], "games": info["games"], "minutes": minutes, "spread": spread}
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"fit-sim-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(meta, indent=1, default=str))
    print(f"\nSaved to {path}")
    if args.write:
        mmod.save_fit(mm or mmod.MinutesModel(), disp, {"season": season, "players": info["players"], "fitted": time.strftime("%Y-%m-%d")})
        print(f"Wrote {mmod.FIT_PATH}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--season")
    ap.add_argument("--board-days", type=int, default=30)
    ap.add_argument("--sample", type=int, default=5000, help="player-games used to fit the spread")
    ap.add_argument("--max-players", type=int, default=None)
    ap.add_argument("--write", action="store_true", help="write app/sim_fit.json")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
