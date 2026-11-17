"""Does a last-season prior make the simulation better?

Read-only against Hardwood (one extra game-log call per player, no model calls, no cost). Run from the project folder:

    python -m scripts.backtest_blend --date 2026-11-14

Compares the current simulation (pull toward the league average only) with versions that also blend in the player's own rates and
minutes from last season, worth at most N minutes of play. Every version scores the same player-game-market records, so the
comparison is paired. Differences carry a 95% interval from a cluster bootstrap (one cluster = one player-game). Scores are raw
simulation probabilities against what happened; this is a test of the simulation, not of the market-beating rule.

A variant is "better" only if its Brier interval against the current model excludes zero. The result is also split by how many
games the player had played, because early season is where a prior should matter.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from scripts.backtest_stage_a import Params, brier, gather, logloss, run_backtest
from scripts.backtest_stage_a2 import cluster_bootstrap_diff

CAPS = [0.0, 300.0, 600.0, 1200.0]
BUCKETS = [("5-7 games", 5, 7), ("8-10 games", 8, 10), ("11+ games", 11, 10_000)]


def slope(p: np.ndarray, y: np.ndarray, iters: int = 50) -> Optional[float]:
    """Logistic calibration slope of y on logit(p). 1 is calibrated; well under 1 means overconfident."""
    x = np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4)))
    if x.std() < 1e-9:
        return None
    a, b = 0.0, 1.0
    for _ in range(iters):
        q = 1 / (1 + np.exp(-(a + b * x)))
        w = q * (1 - q) + 1e-9
        g = np.array([(y - q).sum(), ((y - q) * x).sum()])
        H = np.array([[w.sum(), (w * x).sum()], [(w * x).sum(), (w * x * x).sum()]]) + np.eye(2) * 1e-6
        step = np.linalg.solve(H, g)
        a, b = a + step[0], b + step[1]
    return float(b)


def compare(records_by_cap: Dict[float, List[dict]]) -> dict:
    """Paired comparison of every cap against cap 0 on identical (player, game, market) records."""
    key = lambda r: (r["player_id"], r["game_id"], r["market"])  # noqa: E731
    common = set.intersection(*(set(map(key, rs)) for rs in records_by_cap.values()))
    by = {c: {key(r): r for r in rs if key(r) in common} for c, rs in records_by_cap.items()}
    keys = sorted(common)
    y = np.array([by[0.0][k]["over"] for k in keys], dtype=float)
    cl = np.array([f"{k[0]}|{k[1]}" for k in keys])
    prior = np.array([by[0.0][k]["prior_games"] for k in keys])
    out: dict = {"n": len(keys), "variants": {}}
    base = np.array([by[0.0][k]["p_sim"] for k in keys])
    for cap, recs in by.items():
        p = np.array([recs[k]["p_sim"] for k in keys])
        e, e0 = (p - y) ** 2, (base - y) ** 2
        v = {"brier": float(e.mean()), "brier_fifty": float(((0.5 - y) ** 2).mean()), "logloss": logloss(list(p), list(y.astype(int))),
             "slope": slope(p, y), "mean_abs_tilt": float(np.abs(p - 0.5).mean())}
        if cap > 0:
            d, lo, hi = cluster_bootstrap_diff(e, e0, cl)
            v["brier_diff_vs_current"] = {"diff": d, "lo": lo, "hi": hi}
            v["buckets"] = {}
            for name, a, b in BUCKETS:
                m = (prior >= a) & (prior <= b)
                if m.sum() >= 30:
                    d, lo, hi = cluster_bootstrap_diff(e[m], e0[m], cl[m])
                    v["buckets"][name] = {"n": int(m.sum()), "diff": d, "lo": lo, "hi": hi}
        out["variants"][str(cap)] = v
    return out


def format_report(info: dict, res: dict, covered: int, total: int) -> str:
    L = [f"Last-season prior test: {res['n']:,} paired records; last-season logs found for {covered} of {total} players", ""]
    L.append(f"  {'cap (min)':>9} {'brier':>8} {'50%':>8} {'logloss':>8} {'slope':>6} {'tilt':>6}   brier vs current (95% interval; negative = better)")
    for cap, v in res["variants"].items():
        d = v.get("brier_diff_vs_current")
        diff = f"{d['diff']:+.4f}  [{d['lo']:+.4f}, {d['hi']:+.4f}]" + ("  <- better" if d["hi"] < 0 else "  <- worse" if d["lo"] > 0 else "  (no clear difference)") if d else "(current model)"
        sl = "n/a" if v["slope"] is None else f"{v['slope']:.2f}"
        L.append(f"  {float(cap):>9.0f} {v['brier']:>8.4f} {v['brier_fifty']:>8.4f} {v['logloss']:>8.4f} {sl:>6} {v['mean_abs_tilt']:>6.3f}   {diff}")
    L.append("")
    L.append("By games already played (Brier difference vs current):")
    for cap, v in res["variants"].items():
        for name, b in v.get("buckets", {}).items():
            L.append(f"  cap {float(cap):>5.0f}  {name:10} n={b['n']:>5}  {b['diff']:+.4f}  [{b['lo']:+.4f}, {b['hi']:+.4f}]")
    return "\n".join(L)


async def main_async(args) -> int:
    from app.config import current_season, load_settings, now_et
    from app.hardwood import HardwoodClient, previous_season

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
        sem = asyncio.Semaphore(5)

        async def one(pid: str):
            async with sem:
                res = await hw.post("/query", {"entity": "player", "id": pid, "scope": "game_log", "season": previous_season(season), "season_type": "regular"})
                return pid, (res.get("data") or [])

        got = await asyncio.gather(*(one(p) for p in players), return_exceptions=True)
    finally:
        await hw.aclose()
    last = {r[0]: r[1] for r in got if not isinstance(r, BaseException)}
    covered = sum(1 for v in last.values() if v)
    print(f"Loaded in {time.time() - t0:.0f}s. Running {len(CAPS)} variants...")
    recs = {cap: run_backtest(players, Params(draws=args.draws, seed=args.seed, last_cap_minutes=cap), last=last) for cap in CAPS}
    res = compare(recs)
    print()
    print(format_report(info, res, covered, len(players)))
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest-blend-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"date": date, "season": season, "covered": covered, "players": len(players), "result": res}, indent=1, default=str))
    print(f"\nSaved to {path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date")
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-players", type=int, default=None)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
