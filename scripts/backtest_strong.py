"""Does the green border mean anything? Scores the 'strong' rule on this season's finished boards.

Read-only against Hardwood (about 300 board calls and one game-log call per player, no model calls, no cost). Run from the project folder:

    python -m scripts.backtest_strong

For every finished prop with a closing line, take the market's favored side (from the devigged over/under prices) and ask whether it
hit. Rows are grouped the way the dashboard groups them:
  - top 10%: the prop's market tilt ranks in the top 10% of its market that day (across the WHOLE board, not just the players we simulate)
  - agree / disagree: within the top 10%, the simulation leans the same way as the market, or the other way (this is the 'strong' rule)
  - the rest of the board, and tilt bands, for context.
For each group: hit rate of the favored side with a 95% interval, the hit rate the market's own price implies, and the flat-stake return
at the posted price (one unit on every prop). The agree-versus-disagree difference carries a 95% interval from a cluster bootstrap
(one cluster = one player-game).

Reading it: 'strong' is supported only if agree beats disagree clearly AND beats what the price already implies. If the favored side
simply hits at its implied rate, the market is doing the work and the tilt is not an edge. Lines are the closing lines, not the lines
visible earlier in the day, and results are voided for pushes and DNPs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections import defaultdict
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.signal import add_percentiles, devig, signal
from app.simulation import Params
from scripts.backtest_stage_a import run_backtest

MARKETS = ["pts", "reb", "ast", "fg3m", "pa", "pr", "ra", "pra", "stl", "blk"]
STRONG_PCT = 90.0
BANDS = [("0-50", 0, 50), ("50-70", 50, 70), ("70-90", 70, 90), ("90-95", 90, 95), ("95-100", 95, 100.01)]


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def profit(price: Optional[float], win: bool) -> Optional[float]:
    """Profit on a one-unit stake at an American price."""
    if price is None:
        return None
    price = float(price)
    if not win:
        return -1.0
    return price / 100.0 if price > 0 else 100.0 / abs(price)


def build_rows(boards: List[dict], records: List[dict], stats: Optional[dict] = None) -> List[dict]:
    """boards: rows with date, market, player_id, game_id, line, over_price, under_price. records: backtest records (p_sim, over)."""
    rows = []
    for b in boards:
        s = signal(b.get("over_price"), b.get("under_price"))
        rows.append({"date": b["date"], "market": b["market"], "player_id": str(b["player_id"]), "game_id": str(b["game_id"]), "line": b.get("line"),
                     "strength": s["strength"], "side": (s["side"] or "").upper() or None, "p_over": s["p_over"], "over_price": b.get("over_price"), "under_price": b.get("under_price")})
    # percentile within each date's market, as the dashboard ranks within a day's market
    by = defaultdict(list)
    for r in rows:
        by[(r["date"], r["market"])].append(r)
    for grp in by.values():
        add_percentiles(grp)
    rec = {(str(r["player_id"]), str(r["game_id"]), r["market"]): r for r in records}
    out = []
    for r in rows:
        x = rec.get((r["player_id"], r["game_id"], r["market"]))
        if r["pct"] is None or r["side"] is None or x is None:
            continue                                                  # unfinished, push, DNP, no price or no simulation
        try:
            same = abs(float(r["line"]) - float(x["line"])) < 1e-6
        except (TypeError, ValueError, KeyError):
            same = False
        if stats is not None:
            stats["matched"] = stats.get("matched", 0) + (1 if same else 0)
            stats["line_mismatch"] = stats.get("line_mismatch", 0) + (0 if same else 1)
        if not same:
            continue                                                  # the price belongs to a different threshold than the result was scored against
        over = bool(x["over"])
        fav_over = r["side"] == "OVER"
        sim_side = None if x["p_sim"] == 0.5 else ("OVER" if x["p_sim"] > 0.5 else "UNDER")
        r.update({"hit": over == fav_over, "p_fav": r["p_over"] if fav_over else 1 - r["p_over"],
                  "price": r["over_price"] if fav_over else r["under_price"], "sim_side": sim_side, "p_sim": x["p_sim"], "prior_games": x.get("prior_games"),
                  "p_sim_fav": x["p_sim"] if fav_over else 1 - x["p_sim"], "rec_line": x.get("line"),
                  "sim": "none" if sim_side is None else ("agree" if sim_side == r["side"] else "disagree")})
        out.append(r)
    return out


def group_stats(rows: List[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    k = sum(1 for r in rows if r["hit"])
    lo, hi = wilson(k, n)
    pr = [profit(r["price"], r["hit"]) for r in rows]
    pr = [p for p in pr if p is not None]
    return {"n": n, "hit": k / n, "lo": lo, "hi": hi, "implied": float(np.mean([r["p_fav"] for r in rows])),
            "roi": float(np.mean(pr)) if pr else None}


def _hit(r: dict) -> float:
    return 1.0 if r["hit"] else 0.0


def _gap(r: dict) -> float:
    """Hit minus what the price implied: the part of the result the price does not already explain."""
    return _hit(r) - r["p_fav"]


def _ckey(r: dict, by: str = "player_game") -> str:
    if by == "game":
        return str(r["game_id"])
    if by == "date":
        return str(r["date"])
    return f"{r['player_id']}|{r['game_id']}"


def _cluster_arrays(rows: List[dict], keys: Dict[str, int], val=_hit, by: str = "player_game"):
    k = len(keys)
    s, c = np.zeros(k), np.zeros(k)
    for r in rows:
        i = keys[_ckey(r, by)]
        s[i] += val(r)
        c[i] += 1.0
    return s, c


def diff_ci(a: List[dict], b: List[dict], reps: int = 2000, seed: int = 9, val=_hit, by: str = "player_game") -> Optional[dict]:
    """Difference in the mean of `val` (default: hit rate) between a and b, with a 95% interval, resampling player-games."""
    if not a or not b:
        return None
    keys = {k: i for i, k in enumerate(sorted({_ckey(r, by) for r in a + b}))}
    sa, ca = _cluster_arrays(a, keys, val, by)
    sb, cb = _cluster_arrays(b, keys, val, by)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(reps, len(keys)))
    with np.errstate(invalid="ignore", divide="ignore"):
        d = sa[idx].sum(axis=1) / ca[idx].sum(axis=1) - sb[idx].sum(axis=1) / cb[idx].sum(axis=1)
    d = d[np.isfinite(d)]
    return {"diff": float(sa.sum() / ca.sum() - sb.sum() / cb.sum()), "lo": float(np.percentile(d, 2.5)), "hi": float(np.percentile(d, 97.5))}


def analyze(rows: List[dict]) -> dict:
    top = [r for r in rows if r["pct"] >= STRONG_PCT]
    agree = [r for r in top if r["sim"] == "agree"]
    dis = [r for r in top if r["sim"] == "disagree"]
    out: dict = {"n": len(rows), "groups": {
        "whole board": group_stats(rows), "top 10%": group_stats(top), "top 10% + sim agrees (strong)": group_stats(agree),
        "top 10% + sim disagrees": group_stats(dis), "rest of board": group_stats([r for r in rows if r["pct"] < STRONG_PCT])},
        "bands": {name: group_stats([r for r in rows if lo <= r["pct"] < hi]) for name, lo, hi in BANDS},
        "agree_vs_disagree": diff_ci(agree, dis), "agree_vs_disagree_by_game": diff_ci(agree, dis, by="game"), "strong_vs_rest": diff_ci(agree, [r for r in rows if r["pct"] < STRONG_PCT]),
        "dates": [min(r["date"] for r in rows), max(r["date"] for r in rows)] if rows else None}
    # the same question with the simulation's own confidence, without any market rule: does it hit by itself?
    sim_rows = [r for r in rows if r["sim"] != "none"]
    out["sim_agree_everywhere"] = {"agree": group_stats([r for r in sim_rows if r["sim"] == "agree"]), "disagree": group_stats([r for r in sim_rows if r["sim"] == "disagree"]),
                                   "agree_vs_disagree": diff_ci([r for r in sim_rows if r["sim"] == "agree"], [r for r in sim_rows if r["sim"] == "disagree"])}
    return out


def gap_stats(rows: List[dict]) -> dict:
    """n and the mean gap (hit minus implied), with a 95% interval from resampling player-games."""
    if not rows:
        return {"n": 0}
    keys = {k: i for i, k in enumerate(sorted({f"{r['player_id']}|{r['game_id']}" for r in rows}))}
    sa, ca = _cluster_arrays(rows, keys, _gap)
    rng = np.random.default_rng(5)
    idx = rng.integers(0, len(keys), size=(1000, len(keys)))
    m = sa[idx].sum(axis=1) / ca[idx].sum(axis=1)
    return {"n": len(rows), "gap": float(sa.sum() / ca.sum()), "lo": float(np.percentile(m, 2.5)), "hi": float(np.percentile(m, 97.5))}


def _split(rows: List[dict]) -> Tuple[List[dict], List[dict]]:
    return [r for r in rows if r["sim"] == "agree"], [r for r in rows if r["sim"] == "disagree"]


def _slice(rows: List[dict]) -> dict:
    a, d = _split(rows)
    return {"agree": gap_stats(a), "disagree": gap_stats(d), "diff": diff_ci(a, d, reps=1000, val=_gap) if a and d else None,
            "diff_by_game": diff_ci(a, d, reps=1000, val=_gap, by="game") if a and d else None,
            "diff_by_date": diff_ci(a, d, reps=1000, val=_gap, by="date") if a and d else None}


def robustness(rows: List[dict]) -> dict:
    """Does 'sim agrees with the market' beat what the price already implies, and does it hold up across slices?
    The measure is the gap (hit minus implied probability), so a group with simply higher-priced favorites gets no credit."""
    rs = [r for r in rows if r["sim"] != "none"]
    out: dict = {"overall": _slice(rs), "by_market": {}, "by_half": {}, "by_week": {}, "by_prior_games": {}, "by_sim_lean": {}}
    for m in sorted({r["market"] for r in rs}):
        out["by_market"][m] = _slice([r for r in rs if r["market"] == m])
    dates = sorted({r["date"] for r in rs})
    if dates:
        mid = dates[len(dates) // 2]
        out["by_half"] = {f"through {dates[len(dates) // 2 - 1]}": _slice([r for r in rs if r["date"] < mid]), f"{mid} on": _slice([r for r in rs if r["date"] >= mid])}
        wk = {d: i // 7 for i, d in enumerate(dates)}
        for w in sorted(set(wk.values())):
            ds = [d for d in dates if wk[d] == w]
            out["by_week"][f"{ds[0]} to {ds[-1]}"] = _slice([r for r in rs if wk[r["date"]] == w])
    for name, lo, hi in [("5-8 games", 0, 8), ("9-12 games", 9, 12), ("13+ games", 13, 10_000)]:
        out["by_prior_games"][name] = _slice([r for r in rs if r.get("prior_games") is not None and lo <= r["prior_games"] <= hi])
    # dose-response: the more the sim leans toward the market's favorite, the better the favorite should do relative to its price
    for name, lo, hi in [("sim <40% on the favorite", 0.0, 0.40), ("40-50%", 0.40, 0.50), ("50-60%", 0.50, 0.60), ("60%+", 0.60, 1.01)]:
        out["by_sim_lean"][name] = gap_stats([r for r in rs if lo <= r["p_sim_fav"] < hi])
    return out


def _gs(s: dict) -> str:
    return "n=0" if not s.get("n") else f"{100 * s['gap']:+.1f} [{100 * s['lo']:+.1f}, {100 * s['hi']:+.1f}] (n={s['n']:,})"


def _sl(name: str, s: dict) -> str:
    d = s["diff"]
    verdict = "" if d is None else ("  clearly higher" if d["lo"] > 0 else "  clearly lower" if d["hi"] < 0 else "  no clear difference")
    dd = "n/a" if d is None else f"{100 * d['diff']:+.1f} [{100 * d['lo']:+.1f}, {100 * d['hi']:+.1f}]"
    return f"  {name:26} agree gap {_gs(s['agree']):36} disagree gap {_gs(s['disagree']):36} agree minus disagree {dd}{verdict}"


def _dd(label: str, d: Optional[dict]) -> str:
    return f"    {label:34}" + ("n/a" if not d else f"{100 * d['diff']:+.1f} [{100 * d['lo']:+.1f}, {100 * d['hi']:+.1f}]")


def format_robustness(r: dict) -> str:
    L = ["Does the simulation's agreement beat what the price already implies? (gap = hit rate minus the price's implied rate, in points; 95% intervals)", "",
         _sl("all markets, all dates", r["overall"]),
         "  same difference, resampling whole games / whole dates instead (shared-game and date effects; 15 dates is few, so read the date row loosely):",
         _dd("resampling games", r["overall"]["diff_by_game"]), _dd("resampling dates", r["overall"]["diff_by_date"]), "", "By market"]
    L += [_sl(k, v) for k, v in r["by_market"].items()]
    L += ["", "By half of the dates"] + [_sl(k, v) for k, v in r["by_half"].items()]
    L += ["", "By week"] + [_sl(k, v) for k, v in r["by_week"].items()]
    L += ["", "By the player's games played before this one"] + [_sl(k, v) for k, v in r["by_prior_games"].items()]
    L += ["", "By how strongly the sim favors the market's favorite (gap of the favorite)"] + [f"  {k:26} {_gs(v)}" for k, v in r["by_sim_lean"].items()]
    return "\n".join(L)


def _g(name: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {name:32} n=0"
    roi = "n/a" if s["roi"] is None else f"{100 * s['roi']:+.1f}%"
    return (f"  {name:32} n={s['n']:>6,}  hit {100 * s['hit']:.1f}% [{100 * s['lo']:.1f}, {100 * s['hi']:.1f}]  "
            f"price implies {100 * s['implied']:.1f}%  gap {100 * (s['hit'] - s['implied']):+.1f}  flat-stake return {roi}")


def _d(label: str, d: Optional[dict]) -> str:
    if not d:
        return f"  {label}: not enough rows"
    v = "clearly higher" if d["lo"] > 0 else "clearly lower" if d["hi"] < 0 else "no clear difference"
    return f"  {label}: {100 * d['diff']:+.1f} points [{100 * d['lo']:+.1f}, {100 * d['hi']:+.1f}]  {v}"


def format_report(res: dict) -> str:
    L = [f"Strong-rule check on {res['n']:,} finished props, {res['dates'][0]} to {res['dates'][1]} (favored side of the closing price)", ""]
    for name, s in res["groups"].items():
        L.append(_g(name, s))
    L += ["", "By within-market tilt percentile (is the tilt itself informative?)"]
    for name, s in res["bands"].items():
        L.append(_g("percentile " + name, s))
    L += ["", "Does the simulation's agreement matter?", _d("strong (top 10% + agrees) minus top 10% + disagrees", res["agree_vs_disagree"]),
          _d("strong minus the rest of the board", res["strong_vs_rest"]),
          "  Same question on every prop, any percentile:", _g("sim agrees with market", res["sim_agree_everywhere"]["agree"]),
          _g("sim disagrees with market", res["sim_agree_everywhere"]["disagree"]), _d("agrees minus disagrees", res["sim_agree_everywhere"]["agree_vs_disagree"])]
    return "\n".join(L)


async def fetch_all_boards(hw, dates: List[str]) -> Tuple[List[dict], dict]:
    sem = asyncio.Semaphore(5)
    info: Dict[str, Any] = {"calls": 0, "errors": 0}

    async def one(d: str, m: str):
        async with sem:
            return d, m, await hw.get("/props", {"market": m, "date": d})

    got = await asyncio.gather(*(one(d, m) for d in dates for m in MARKETS), return_exceptions=True)
    rows: List[dict] = []
    for g in got:
        info["calls"] += 1
        if isinstance(g, BaseException):
            info["errors"] += 1
            continue
        d, m, body = g
        for r in body.get("data") or []:
            if r.get("player_id") and r.get("game_id") and r.get("line") is not None:
                rows.append({**r, "date": d, "market": m})
    info["board_rows"] = len(rows)
    return rows, info


async def main_async(args) -> int:
    from app.config import current_season, load_settings, now_et
    from app.hardwood import HardwoodClient

    s = load_settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    if args.rows:
        rows = json.loads(Path(args.rows).read_text())
        print(f"{len(rows):,} rows from {args.rows}")
        print(); print(format_report(analyze(rows))); print(); print(format_robustness(robustness(rows)))
        return 0
    today = now_et().date()
    start = _date.fromisoformat(args.start)
    dates = [(start + timedelta(days=k)).isoformat() for k in range((today - start).days)]      # finished days only (not today)
    season = current_season(now_et())
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    t0 = time.time()
    try:
        print(f"Loading {len(dates)} boards x {len(MARKETS)} markets...")
        boards, binfo = await fetch_all_boards(hw, dates)
        ids = sorted({str(b["player_id"]) for b in boards})
        names = {str(b["player_id"]): b.get("name") or "" for b in boards}
        print(f"{binfo['board_rows']:,} board rows, {len(ids)} players, {binfo['errors']} errors in {time.time() - t0:.0f}s. Loading game logs...")
        sem = asyncio.Semaphore(5)

        async def one(pid: str):
            async with sem:
                res = await hw.post("/query", {"entity": "player", "id": pid, "scope": "game_log", "season": season, "season_type": "regular"})
                return pid, (res.get("data") or [])

        got = await asyncio.gather(*(one(p) for p in ids), return_exceptions=True)
    finally:
        await hw.aclose()
    players = {r[0]: (names.get(r[0], ""), r[1]) for r in got if not isinstance(r, BaseException) and r[1]}
    print(f"{len(players)} game logs. Simulating...")
    records = run_backtest(players, Params(draws=args.draws))
    jstats: dict = {}
    rows = build_rows(boards, records, jstats)
    print(f"Joined board prices to simulated results: {jstats.get('matched', 0):,} with the same line, {jstats.get('line_mismatch', 0):,} dropped for a different line.")
    res = analyze(rows)
    rob = robustness(rows)
    print()
    print(format_report(res))
    print()
    print(format_robustness(rob))
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    path = out_dir / f"backtest-strong-{stamp}.json"
    path.write_text(json.dumps({"season": season, "boards": binfo, "players": len(players), "result": res, "robustness": rob}, indent=1, default=str))
    rpath = out_dir / f"backtest-strong-rows-{stamp}.json"
    rpath.write_text(json.dumps(rows, default=str))
    print(f"\nSaved to {path}\nRows saved to {rpath} (reuse with --rows)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", default="2026-10-20", help="first board date (season opener)")
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--rows", help="reuse the rows file written by an earlier run instead of reloading from Hardwood")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
