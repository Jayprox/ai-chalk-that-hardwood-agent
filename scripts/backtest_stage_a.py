"""Stage A backtest: does a minutes-times-rate simulation say anything useful about prop results?

Read-only against Hardwood (about 200 calls, no model calls, no cost). Run from the project folder with the venv active:

    pip install -r requirements-dev.txt          # adds numpy, once
    python -m scripts.backtest_stage_a           # players on today's US Eastern boards
    python -m scripts.backtest_stage_a --date 2026-11-13 --draws 4000

What it does
  1. Finds the players on the date's prop boards (points, rebounds, assists, threes, steals, blocks).
  2. Loads each player's season game log. Every game row carries the closing line and the result for each market.
  3. Integrity check: recomputes each result from the actual stat and the line and compares it with Hardwood's result.
  4. For every past prop, simulates using ONLY that player's earlier games: minutes ~ Normal, then each stat ~ Negative
     Binomial around (shrunken per-minute rate x minutes). Combo markets add the same draws, so they stay consistent.
  5. Scores the simulated P(over) against what happened, next to two baselines: 50 percent, and the player's own earlier
     over-rate on that market.

Known simplifications (stated so the result is read correctly)
  - League per-minute rates and the dispersion of each stat are estimated from the whole season's data: a small look-ahead
    on a handful of league-level numbers. Each player's own minutes and rates use earlier games only.
  - No opponent, pace, rest or teammate adjustments yet. This is the plain baseline.
  - Conditional on the player playing; games he did not play are skipped.
  - The closing line already holds the market's view, so a probability near 50 percent is the market's own number. Beating
    50 percent on Brier score means the simulation adds information beyond the line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from app.simulation import (STAT_KEYS, MARKET_STATS, Params, League, league_params, simulate_player, prob_over, is_played as _played)  # noqa: F401

BOARD_MARKETS = ["pts", "reb", "ast", "fg3m", "stl", "blk"]  # used only to find which players to load


# --------------------------------------------------------------------------- results and integrity

def actual_for(market: str, game: dict) -> Optional[float]:
    parts = MARKET_STATS.get(market)
    if not parts:
        return None
    vals = [game.get(p) for p in parts]
    if any(v is None for v in vals):
        return None
    return float(sum(vals))


def outcome(actual: float, line: float) -> str:
    if actual > line:
        return "over"
    if actual < line:
        return "under"
    return "push"



def check_results(players: Dict[str, Tuple[str, List[dict]]], max_samples: int = 6) -> dict:
    """Recompute each result from the stat and the line and compare it with Hardwood's stored result."""
    checked = mismatched = unmapped = no_result = dnp_with_props = 0
    samples: List[dict] = []
    for pid, (name, logs) in players.items():
        for g in logs:
            for market, p in (g.get("props") or {}).items():
                line, given = (p or {}).get("line"), (p or {}).get("result")
                if not _played(g):
                    dnp_with_props += 1
                    continue
                actual = actual_for(market, g)
                if actual is None or line is None:
                    unmapped += 1
                    continue
                if given is None:
                    no_result += 1
                    continue
                checked += 1
                mine = outcome(actual, float(line))
                if mine != given:
                    mismatched += 1
                    if len(samples) < max_samples:
                        samples.append({"player": name, "date": g.get("date"), "market": market, "line": line,
                                        "actual": actual, "hardwood_result": given, "recomputed": mine})
    return {"checked": checked, "mismatched": mismatched, "market_not_mapped_or_no_line": unmapped,
            "no_stored_result": no_result, "props_on_games_not_played": dnp_with_props, "samples": samples}


def run_backtest(players: Dict[str, Tuple[str, List[dict]]], params: Params, lg: Optional[League] = None,
                 last: Optional[Dict[str, List[dict]]] = None) -> List[dict]:
    rng = np.random.default_rng(params.seed)
    sorted_players = {pid: (name, sorted(logs, key=lambda g: g.get("date") or "")) for pid, (name, logs) in players.items()}
    lg = lg or league_params(logs for _n, logs in sorted_players.values())
    records: List[dict] = []
    for pid, (name, logs) in sorted_players.items():
        for i, g in enumerate(logs):
            props = g.get("props") or {}
            if not props or not _played(g):
                continue
            prior = logs[:i]
            sim = simulate_player(prior, lg, params, rng, (last or {}).get(pid))
            if sim is None:
                continue
            for market, p in props.items():
                line = (p or {}).get("line")
                actual = actual_for(market, g)
                if line is None or actual is None:
                    continue
                real = outcome(actual, float(line))
                if real == "push":
                    continue
                total = sum(sim[k] for k in MARKET_STATS[market])
                p_sim = prob_over(total, float(line))
                if p_sim is None:
                    continue
                over = under = 0
                for pg in prior:
                    pp = (pg.get("props") or {}).get(market) or {}
                    a = actual_for(market, pg) if _played(pg) else None
                    if a is None or pp.get("line") is None:
                        continue
                    o = outcome(a, float(pp["line"]))
                    over += o == "over"
                    under += o == "under"
                played_prior = [x["minutes"] for x in prior if _played(x)]
                last3 = played_prior[-3:]
                records.append({
                    "player": name, "date": g.get("date"), "market": market, "line": float(line), "actual": actual,
                    "over": 1 if real == "over" else 0, "p_sim": p_sim, "p_hitrate": (over + 1) / (over + under + 2),
                    "prior_games": len(played_prior),
                    # extra fields for the Stage A2 feature test
                    "player_id": pid, "game_id": g.get("game_id"), "venue": g.get("venue"), "rest_days": g.get("rest_days"),
                    "started": g.get("started"), "sim_mean": float(total.mean()), "sim_sd": float(total.std()),
                    "min_trend": float(sum(last3) / len(last3) - sum(played_prior) / len(played_prior)),
                })
    return records


# --------------------------------------------------------------------------- scoring

def brier(ps: List[float], ys: List[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys)


def logloss(ps: List[float], ys: List[int]) -> float:
    eps = 1e-3
    return -sum(y * math.log(min(max(p, eps), 1 - eps)) + (1 - y) * math.log(1 - min(max(p, eps), 1 - eps))
                for p, y in zip(ps, ys)) / len(ys)


BINS = [(0.0, 0.30), (0.30, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.70), (0.70, 1.01)]


def calibration(records: List[dict], key: str = "p_sim") -> List[dict]:
    rows = []
    for lo, hi in BINS:
        sel = [r for r in records if lo <= r[key] < hi]
        if not sel:
            rows.append({"bin": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": 0})
            continue
        hit = sum(r["over"] for r in sel) / len(sel)
        rows.append({"bin": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(sel), "mean_predicted": sum(r[key] for r in sel) / len(sel),
                     "actual_over_rate": hit, "std_error": math.sqrt(max(hit * (1 - hit), 1e-9) / len(sel))})
    return rows


def summarize(records: List[dict]) -> dict:
    if not records:
        return {"n": 0}
    ys = [r["over"] for r in records]
    sim = [r["p_sim"] for r in records]
    hr = [r["p_hitrate"] for r in records]
    half = [0.5] * len(records)
    out: dict = {
        "n": len(records),
        "over_rate": sum(ys) / len(ys),
        "mean_p_sim": sum(sim) / len(sim),
        "brier": {"sim": brier(sim, ys), "hitrate_baseline": brier(hr, ys), "fifty_percent": brier(half, ys)},
        "logloss": {"sim": logloss(sim, ys), "hitrate_baseline": logloss(hr, ys), "fifty_percent": logloss(half, ys)},
        "calibration_sim": calibration(records, "p_sim"),
    }
    hi = [r for r in records if r["p_sim"] >= 0.60]
    lo = [r for r in records if r["p_sim"] <= 0.40]
    out["strong_over"] = {"n": len(hi), "hit_rate": (sum(r["over"] for r in hi) / len(hi)) if hi else None}
    out["strong_under"] = {"n": len(lo), "hit_rate": (sum(1 - r["over"] for r in lo) / len(lo)) if lo else None}
    by_market = {}
    for m in sorted({r["market"] for r in records}):
        sel = [r for r in records if r["market"] == m]
        y = [r["over"] for r in sel]
        by_market[m] = {"n": len(sel), "over_rate": sum(y) / len(y), "mean_p_sim": sum(r["p_sim"] for r in sel) / len(sel),
                        "brier_sim": brier([r["p_sim"] for r in sel], y), "brier_fifty": brier([0.5] * len(sel), y)}
    out["by_market"] = by_market
    return out


# --------------------------------------------------------------------------- loading (Hardwood)

async def gather(hw, date: str, season: str, max_players: Optional[int] = None) -> Tuple[Dict[str, Tuple[str, List[dict]]], dict]:
    info: Dict[str, Any] = {"board_markets": {}, "player_id_source": None, "errors": []}
    found: Dict[str, str] = {}  # id -> name
    teams: Dict[str, str] = {}  # id -> team abbreviation on the board
    info["teams"] = teams
    unresolved: Dict[str, str] = {}  # name -> team
    for m in BOARD_MARKETS:
        try:
            res = await hw.get("/props", {"market": m, "date": date})
        except Exception as e:
            info["errors"].append(f"board {m}: {type(e).__name__}")
            continue
        rows = res.get("data") or []
        info["board_markets"][m] = len(rows)
        if rows and info["player_id_source"] is None:
            info["row_fields"] = sorted(rows[0].keys())
        for r in rows:
            pid = r.get("player_id") or r.get("id")
            if pid:
                found[str(pid)] = r.get("name") or str(pid)
                if r.get("team"):
                    teams[str(pid)] = r["team"]
                info["player_id_source"] = "board row"
            elif r.get("name"):
                unresolved[r["name"]] = r.get("team") or ""
    if unresolved:
        info["player_id_source"] = info["player_id_source"] or "name lookup"
        known = set(found.values())
        for name in sorted(unresolved):
            if name in known:
                continue
            try:
                p = await hw.resolve_player(name)
                found[str(p["id"])] = name
                if unresolved.get(name):
                    teams[str(p["id"])] = unresolved[name]
            except Exception:
                info["errors"].append(f"could not resolve {name}")
    ids = sorted(found)[: max_players or None]
    sem = asyncio.Semaphore(5)

    async def one(pid: str):
        async with sem:
            res = await hw.post("/query", {"entity": "player", "id": pid, "scope": "game_log", "season": season, "season_type": "regular"})
            return pid, (res.get("data") or [])

    results = await asyncio.gather(*(one(p) for p in ids), return_exceptions=True)
    players: Dict[str, Tuple[str, List[dict]]] = {}
    for pid, res in zip(ids, results):
        if isinstance(res, BaseException):
            info["errors"].append(f"log {found[pid]}: {type(res).__name__}")
            continue
        players[pid] = (found[pid], res[1])
    info["players_loaded"] = len(players)
    info["games_loaded"] = sum(len(v[1]) for v in players.values())
    return players, info


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def format_report(info: dict, integrity: dict, summary: dict, params: Params) -> str:
    L: List[str] = []
    L.append(f"Players loaded {info.get('players_loaded')}, games {info.get('games_loaded')}. Board rows by market: {info.get('board_markets')}")
    if info.get("errors"):
        L.append(f"Load errors ({len(info['errors'])}): {info['errors'][:5]}")
    L.append("")
    L.append("INTEGRITY: Hardwood's stored result vs result recomputed from stat and line")
    L.append(f"  checked {integrity['checked']}, mismatched {integrity['mismatched']}, no stored result {integrity['no_stored_result']}, "
             f"market not mapped or no line {integrity['market_not_mapped_or_no_line']}, props on games not played {integrity['props_on_games_not_played']}")
    for s in integrity["samples"]:
        L.append(f"  MISMATCH {s}")
    L.append("")
    if not summary.get("n"):
        L.append("No backtest rows (not enough earlier games per player).")
        return "\n".join(L)
    b, ll = summary["brier"], summary["logloss"]
    L.append(f"BACKTEST: {summary['n']} prop results, each simulated from earlier games only "
             f"(min {params.min_prior} earlier games, {params.draws} draws, prior {params.prior_minutes:g} min)")
    L.append(f"  Over rate in the data {_pct(summary['over_rate'])}; mean simulated P(over) {_pct(summary['mean_p_sim'])}")
    L.append(f"  Brier (lower is better):    sim {b['sim']:.4f} | player's earlier over-rate {b['hitrate_baseline']:.4f} | 50% {b['fifty_percent']:.4f}")
    L.append(f"  Log loss (lower is better): sim {ll['sim']:.4f} | player's earlier over-rate {ll['hitrate_baseline']:.4f} | 50% {ll['fifty_percent']:.4f}")
    L.append("")
    L.append("CALIBRATION (simulated P(over) vs how often the over actually hit)")
    L.append(f"  {'bin':10} {'n':>6} {'predicted':>10} {'actual':>8} {'+/- se':>8}")
    for r in summary["calibration_sim"]:
        if r["n"]:
            L.append(f"  {r['bin']:10} {r['n']:>6} {_pct(r['mean_predicted']):>10} {_pct(r['actual_over_rate']):>8} {100 * r['std_error']:>7.1f}%")
        else:
            L.append(f"  {r['bin']:10} {0:>6}")
    so, su = summary["strong_over"], summary["strong_under"]
    L.append("")
    L.append(f"  Sim says over >= 60%: n {so['n']}, over hit {_pct(so['hit_rate'])}")
    L.append(f"  Sim says over <= 40%: n {su['n']}, under hit {_pct(su['hit_rate'])}")
    L.append("")
    L.append("BY MARKET")
    L.append(f"  {'market':7} {'n':>6} {'over rate':>10} {'mean sim':>9} {'brier sim':>10} {'brier 50%':>10}")
    for m, r in summary["by_market"].items():
        L.append(f"  {m:7} {r['n']:>6} {_pct(r['over_rate']):>10} {_pct(r['mean_p_sim']):>9} {r['brier_sim']:>10.4f} {r['brier_fifty']:>10.4f}")
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
    params = Params(min_prior=args.min_prior, prior_minutes=args.prior_minutes, draws=args.draws, seed=args.seed)
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    t0 = time.time()
    try:
        print(f"Date {date}, season {season}. Loading boards and game logs...")
        players, info = await gather(hw, date, season, args.max_players)
    finally:
        await hw.aclose()
    print(f"Loaded in {time.time() - t0:.0f}s. Running the simulation...")
    integrity = check_results(players)
    records = run_backtest(players, params)
    summary = summarize(records)
    print()
    print(format_report(info, integrity, summary, params))
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest-stage-a-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"date": date, "season": season, "params": params.__dict__, "info": info, "integrity": integrity,
                                "summary": summary, "records": records}, indent=1, default=str))
    print(f"\nSaved to {path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="US Eastern date whose boards pick the players (default today)")
    ap.add_argument("--draws", type=int, default=4000)
    ap.add_argument("--min-prior", type=int, default=5)
    ap.add_argument("--prior-minutes", type=float, default=100.0)
    ap.add_argument("--max-players", type=int, default=None, help="cap for a quick trial run")
    ap.add_argument("--seed", type=int, default=7)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
