"""Habit screen: which pre-game facts improve the simulation's stat prediction?

Read-only against Hardwood (a few hundred calls, no model calls, no cost). Run from the project folder:

    python -m scripts.habit_screen

Uses last season's full game logs, which need no betting lines. For every game from a player's 6th of the season on, it takes what the
current simulation would have predicted (his shrunk per-minute rate times his average minutes, from EARLIER games only) and asks
whether a group of facts known before tip-off explains the miss: schedule (home, back-to-back, rest, TV, altitude), role (starter
rate, minutes trend and volatility), recent form, how the opponent has treated other players, the player's own history against this
opponent, and blowout / foul tendencies.

Honest validation: ridge regressions on the miss, fitted on earlier dates and scored on later dates (forward chaining), with a 95%
interval on the squared-error difference from a cluster bootstrap (one cluster = one player). A group "helps" only if its whole
interval is below zero. Targets are minutes, points, rebounds, assists and threes. This measures stat accuracy, NOT whether the
simulation beats the betting market; that still needs the lines (Stage B re-run).

It also checks the simulation's spread: does its central 80% range hold about 80% of outcomes?
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.simulation import League, Params, STAT_KEYS, is_played, league_params, simulate_player
from scripts.backtest_stage_a2 import cluster_bootstrap_diff

TARGETS = ["min", "pts", "reb", "ast", "fg3m"]
GROUPS = {
    "schedule": ["home", "b2b", "rest", "player_rest", "altitude", "tv_major"],
    "role": ["started_rate", "min_trend", "min_sd"],
    "form": ["form"],
    "opponent": ["opp"],
    "head_to_head": ["h2h"],
    "blowout_fouls": ["pm_abs", "pf_rate"],
}
OPP_K, H2H_K = 20.0, 3.0   # shrinkage (pseudo-games) for the opponent and head-to-head residual means


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def build_rows(players: Dict[str, Tuple[str, List[dict]]], lg: League, params: Params, min_prior: int = 6, coverage_draws: int = 0,
               seed: int = 5) -> List[dict]:
    """One row per player-game with the baseline prediction, the actual, and the as-of-game features. Opponent and head-to-head
    features use only rows from strictly earlier dates."""
    rows: List[dict] = []
    rng = np.random.default_rng(seed)
    for pid, (_name, logs) in players.items():
        played = sorted((g for g in logs if is_played(g)), key=lambda g: g.get("date") or "")
        for i in range(min_prior, len(played)):
            prior, g = played[:i], played[i]
            mins = np.array([x["minutes"] for x in prior], dtype=float)
            tot, mean_min = float(mins.sum()), float(mins.mean())
            row: Dict[str, Any] = {"pid": pid, "date": g.get("date"), "opp": g.get("opponent"), "act": {"min": float(g["minutes"])}, "base": {"min": mean_min}, "f": {}}
            f = row["f"]
            last5 = prior[-5:]
            for k in ("pts", "reb", "ast", "fg3m"):
                s = float(sum(_num(x.get(k)) for x in prior))
                rate = (s + params.prior_minutes * lg.rate[k]) / (tot + params.prior_minutes)
                row["base"][k] = rate * mean_min
                row["act"][k] = _num(g.get(k))
                m5 = float(sum(x["minutes"] for x in last5))
                s5 = float(sum(_num(x.get(k)) for x in last5))
                f["form_" + k] = ((s5 + params.prior_minutes * 0.15 * lg.rate[k]) / (m5 + params.prior_minutes * 0.15) - rate) * mean_min
            f["home"] = 1.0 if g.get("venue") == "home" else 0.0
            f["b2b"] = 1.0 if (g.get("player_b2b_night") or g.get("b2b_night")) else 0.0
            f["rest"] = min(_num(g.get("rest_days"), 2.0), 7.0)
            f["player_rest"] = min(_num(g.get("player_rest_days"), 2.0), 7.0)
            f["altitude"] = 1.0 if g.get("altitude") else 0.0
            f["tv_major"] = 1.0 if str(g.get("national_tv_tier") or "").lower() in ("major", "national") else 0.0
            known = [x.get("started") for x in prior if x.get("started") is not None]
            f["started_rate"] = float(np.mean([1.0 if v else 0.0 for v in known])) if known else 0.5
            f["min_trend"] = float(np.mean(mins[-3:]) - mean_min)
            f["min_sd"] = float(mins.std(ddof=1)) if len(mins) > 1 else 0.0
            f["pm_abs"] = float(np.mean([abs(_num(x.get("plus_minus"))) for x in prior]))
            f["pf_rate"] = float(sum(_num(x.get("pf")) for x in prior) / max(tot, 1.0) * 36.0)
            if coverage_draws:
                d = simulate_player(prior, lg, Params(draws=coverage_draws, min_prior=params.min_prior, prior_minutes=params.prior_minutes,
                                                     min_sd=params.min_sd), rng)
                if d is not None:
                    row["pit"] = {}
                    for k in ("pts", "reb", "ast", "fg3m"):
                        a = row["act"][k]
                        row["pit"][k] = float((d[k] < a).mean() + rng.uniform() * (d[k] == a).mean())
            rows.append(row)
    # opponent and head-to-head residual means, strictly from earlier dates
    rows.sort(key=lambda r: r["date"] or "")
    opp: Dict[Tuple[str, str], List[float]] = defaultdict(lambda: [0.0, 0.0])
    h2h: Dict[Tuple[str, str, str], List[float]] = defaultdict(lambda: [0.0, 0.0])
    i = 0
    while i < len(rows):
        j = i
        while j < len(rows) and rows[j]["date"] == rows[i]["date"]:
            j += 1
        for r in rows[i:j]:
            for t in TARGETS:
                so, no = opp[(r["opp"], t)]
                sh, nh = h2h[(r["pid"], r["opp"], t)]
                r["f"]["opp_" + t] = so / (no + OPP_K)
                r["f"]["h2h_" + t] = sh / (nh + H2H_K)
        for r in rows[i:j]:
            for t in TARGETS:
                res = r["act"][t] - r["base"][t]
                opp[(r["opp"], t)][0] += res; opp[(r["opp"], t)][1] += 1
                h2h[(r["pid"], r["opp"], t)][0] += res; h2h[(r["pid"], r["opp"], t)][1] += 1
        i = j
    return rows


def design(rows: List[dict], target: str, group: str) -> np.ndarray:
    cols = []
    for name in GROUPS[group]:
        if name == "form":
            cols.append([r["f"].get("form_" + target, 0.0) if target != "min" else 0.0 for r in rows])
        elif name in ("opp", "h2h"):
            cols.append([r["f"][name + "_" + target] for r in rows])
        else:
            cols.append([r["f"][name] for r in rows])
    X = np.array(cols, dtype=float).T
    keep = X.std(axis=0) > 1e-12 if len(X) else np.array([], dtype=bool)
    return X[:, keep] if X.size else X


def ridge_predict(Xtr: np.ndarray, ytr: np.ndarray, Xte: np.ndarray, lam: float = 50.0) -> np.ndarray:
    if Xtr.shape[1] == 0:
        return np.zeros(len(Xte))
    mu, sd = Xtr.mean(axis=0), Xtr.std(axis=0) + 1e-9
    A, B = (Xtr - mu) / sd, (Xte - mu) / sd
    ym = ytr.mean()
    w = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ (ytr - ym))
    return B @ w + ym


def screen(rows: List[dict], blocks: int = 4, reps: int = 1000, lam: float = 50.0) -> dict:
    dates = sorted({r["date"] for r in rows})
    edges = np.array_split(np.array(dates), blocks)
    block_of = {d: i for i, grp in enumerate(edges) for d in grp}
    blk = np.array([block_of[r["date"]] for r in rows])
    cl = np.array([r["pid"] for r in rows])
    out: dict = {"n": len(rows), "test_n": int((blk >= 1).sum()), "targets": {}}
    for t in TARGETS:
        y = np.array([r["act"][t] - r["base"][t] for r in rows])
        test = blk >= 1
        base_err = y[test] ** 2
        res: dict = {"mse_baseline": float(base_err.mean()), "bias": float(y.mean()), "groups": {}}
        for g in list(GROUPS) + ["all"]:
            X = np.hstack([design(rows, t, gg) for gg in (GROUPS if g == "all" else [g])])
            pred = np.zeros(len(rows))
            for k in range(1, blocks):
                tr, te = blk < k, blk == k
                if te.sum() == 0 or tr.sum() < 100:
                    continue
                pred[te] = ridge_predict(X[tr], y[tr], X[te], lam)
            err = (y[test] - pred[test]) ** 2
            d, lo, hi = cluster_bootstrap_diff(err, base_err, cl[test], reps)
            res["groups"][g] = {"mse": float(err.mean()), "diff": d, "lo": lo, "hi": hi, "pct": 100 * d / float(base_err.mean()),
                                "verdict": "helps" if hi < 0 else "hurts" if lo > 0 else "no clear difference"}
        out["targets"][t] = res
    pit = [r["pit"] for r in rows if "pit" in r]
    if pit:
        cov = {}
        for k in ("pts", "reb", "ast", "fg3m"):
            u = np.array([p[k] for p in pit])
            cov[k] = {"central80": float(((u >= 0.1) & (u <= 0.9)).mean()), "deciles": [float(((u >= a / 10) & (u < (a + 1) / 10)).mean()) for a in range(10)], "n": len(u)}
        out["coverage"] = cov
    return out


def format_report(res: dict, players: int) -> str:
    L = [f"Habit screen: {res['n']:,} player-games from {players} players; scored on the later {res['test_n']:,} (forward chaining)", ""]
    L.append("Squared-error change vs the current simulation's prediction (negative = better; 95% interval; 'helps' = whole interval below zero)")
    for t, r in res["targets"].items():
        L.append(f"\n{t.upper()}  baseline MSE {r['mse_baseline']:.2f}, mean miss (actual minus predicted) {r['bias']:+.2f}")
        for g, v in r["groups"].items():
            L.append(f"  {g:14} {v['pct']:+6.2f}%  [{v['lo']:+.3f}, {v['hi']:+.3f}]  {v['verdict']}")
    if "coverage" in res:
        L.append("\nDoes the simulation's central 80% range hold about 80% of outcomes?")
        for k, c in res["coverage"].items():
            L.append(f"  {k:5} {100 * c['central80']:.0f}% (n={c['n']:,});  tails: below the 10th pct {100 * c['deciles'][0]:.0f}%, above the 90th {100 * c['deciles'][9]:.0f}% (calibrated = 10% each)")
    return "\n".join(L)


async def gather_players(hw, season: str, dates: List[str], max_players: Optional[int] = None) -> Tuple[Dict[str, Tuple[str, List[dict]]], dict]:
    info: dict = {"errors": []}
    found: Dict[str, str] = {}
    for d in dates:
        for m in ("pts", "ast", "reb"):
            try:
                res = await hw.get("/props", {"market": m, "date": d})
            except Exception as e:  # noqa: BLE001
                info["errors"].append(f"board {m} {d}: {type(e).__name__}")
                continue
            for r in res.get("data") or []:
                if r.get("player_id"):
                    found.setdefault(r["player_id"], r.get("name") or r["player_id"])
    ids = sorted(found)[: max_players or None]
    sem = asyncio.Semaphore(5)

    async def one(pid: str):
        async with sem:
            res = await hw.post("/query", {"entity": "player", "id": pid, "scope": "game_log", "season": season, "season_type": "regular"})
            return pid, (res.get("data") or [])

    players: Dict[str, Tuple[str, List[dict]]] = {}
    for pid, res in zip(ids, await asyncio.gather(*(one(p) for p in ids), return_exceptions=True)):
        if isinstance(res, BaseException):
            info["errors"].append(f"log {found[pid]}: {type(res).__name__}")
        else:
            players[pid] = (found[pid], res[1])
    info["players"] = len(players)
    info["games"] = sum(len(v[1]) for v in players.values())
    return players, info


async def main_async(args) -> int:
    from datetime import date as _d, timedelta
    from app.config import current_season, load_settings, now_et
    from app.hardwood import HardwoodClient, previous_season

    s = load_settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    today = now_et().date()
    dates = [(today - timedelta(days=k)).isoformat() for k in range(0, args.board_days, 3)]
    last = previous_season(current_season(now_et()))
    seasons = [x.strip() for x in args.seasons.split(",")] if args.seasons else [last]
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    t0 = time.time()
    rows: List[dict] = []
    info = {"players": 0, "games": 0, "seasons": {}}
    try:
        for season in seasons:
            print(f"Season {season}. Collecting players from {len(dates)} recent boards, then their game logs...")
            players, si = await gather_players(hw, season, dates, args.max_players)
            lg = league_params(logs for _n, logs in players.values())
            part = build_rows(players, lg, Params(), coverage_draws=args.coverage_draws)
            rows += part
            info["seasons"][season] = {"players": si["players"], "games": si["games"], "rows": len(part)}
            info["players"] = max(info["players"], si["players"])
            info["games"] += si["games"]
    finally:
        await hw.aclose()
    season = ",".join(seasons)
    per = ", ".join("%s: %d players, %s scored" % (k, v["players"], format(v["rows"], ",")) for k, v in info["seasons"].items())
    print(f"Loaded {info['games']:,} games in {time.time() - t0:.0f}s ({per}). Screening...")
    res = screen(rows, args.blocks)
    print()
    print(format_report(res, info["players"]))
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"habit-screen-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"season": season, "info": info, "result": res}, indent=1, default=str))
    print(f"\nSaved to {path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seasons", help="comma-separated, e.g. 2023-24,2024-25,2025-26 (default: last season only); pooled, scored forward in time")
    ap.add_argument("--board-days", type=int, default=30, help="how far back to look for boards that name players (every 3rd day)")
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--coverage-draws", type=int, default=1000)
    ap.add_argument("--max-players", type=int, default=None)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
