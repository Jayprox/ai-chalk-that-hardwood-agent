"""Stage B: do teammate absences add skill beyond the market price?

Read-only against Hardwood (about 300 calls, no model calls, no cost). Run from the project folder with the venv active:

    python -m scripts.backtest_stage_b --date 2026-11-13

Why: injury news is the main information the closing line has and our simulation lacks. This asks two questions.
  1. Mechanism: when a rotation teammate does not play, do the others actually play more minutes than their own earlier average?
  2. Outcome: does knowing which rotation teammates sat add predictive skill beyond the market's price, tested out of sample?

How "absent" is decided: a teammate counts as an absent rotation player in a team game if, before that game, he had played at least
5 games averaging at least 15 minutes for this team's data set, and he has no played row for that game. Starters are those who
started at least half of their earlier games.

Read this as an UPPER BOUND: the test uses who actually did not play, including late scratches. The closing line knew most of it,
and our live pipeline would only know what the web search finds. If perfect knowledge of absences adds nothing beyond the price, the
idea is dead. If it adds something, a second step must show that live availability data is good enough to capture it.

Limits: players traded this season can look absent for games played before they joined; only teammates who appear on today's boards
are tracked; the sample is the same 14 game days as before.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from scripts.backtest_stage_a import Params, _played, gather, run_backtest
from scripts.backtest_stage_a2 import MARKETS, analyze, attach_board, build_features, fetch_boards, format_report

MODELS_B: List[Tuple[str, List[str]]] = [
    ("market, calibrated", ["logit_mkt"]),
    ("market + absences", ["logit_mkt", "absent_min", "absent_starters"]),
    ("market + sim", ["logit_mkt", "z_sim"]),
    ("market + sim + absences", ["logit_mkt", "z_sim", "absent_min", "absent_starters"]),
]


def absence_features(players: Dict[str, Tuple[str, List[dict]]], teams: Dict[str, str], min_minutes: float = 15.0,
                     min_games: int = 5) -> Tuple[Dict[Tuple[str, str], dict], dict]:
    """(player_id, game_id) -> the rotation teammates who did not play that game, and how many minutes they normally play."""
    by_team: Dict[str, List[str]] = defaultdict(list)
    for pid in players:
        if teams.get(pid):
            by_team[teams[pid]].append(pid)
    out: Dict[Tuple[str, str], dict] = {}
    for team, pids in by_team.items():
        played = {p: {g["game_id"]: g for g in players[p][1] if g.get("game_id") and _played(g)} for p in pids}
        team_games: Dict[str, str] = {}
        for p in pids:
            for gid, g in played[p].items():
                team_games[gid] = g["date"]
        for gid, date in team_games.items():
            absent = []
            for q in pids:
                prior = [g for g in played[q].values() if g["date"] < date]
                if len(prior) < min_games:
                    continue
                mean = sum(g["minutes"] for g in prior) / len(prior)
                if mean < min_minutes or gid in played[q]:
                    continue
                starts = sum(1 for g in prior if g.get("started")) / len(prior)
                absent.append((q, players[q][0], mean, starts))
            for p in pids:
                if gid not in played[p]:
                    continue
                mine = [a for a in absent if a[0] != p]
                out[(p, gid)] = {"absent_min": sum(a[2] for a in mine), "absent_starters": sum(1 for a in mine if a[3] >= 0.5),
                                 "absent_names": [a[1] for a in mine]}
    values = list(out.values())
    summary = {"player_games": len(values), "with_any_absent": sum(1 for v in values if v["absent_names"]),
               "with_absent_starter": sum(1 for v in values if v["absent_starters"] > 0),
               "teams": len(by_team)}
    return out, summary


def minutes_mechanism(players: Dict[str, Tuple[str, List[dict]]], feats: Dict[Tuple[str, str], dict], min_games: int = 5,
                      reps: int = 1000, seed: int = 13) -> dict:
    """Does a teammate's absence move a player's minutes away from his own earlier average?"""
    xs, ys, who = [], [], []
    for pid, (_name, logs) in players.items():
        logs = sorted(logs, key=lambda g: g.get("date") or "")
        for i, g in enumerate(logs):
            if not _played(g) or (pid, g.get("game_id")) not in feats:
                continue
            prior = [x["minutes"] for x in logs[:i] if _played(x)]
            if len(prior) < min_games:
                continue
            xs.append(feats[(pid, g["game_id"])]["absent_min"])
            ys.append(g["minutes"] - sum(prior) / len(prior))
            who.append(pid)
    if len(xs) < 50:
        return {"n": len(xs)}
    x, y = np.array(xs), np.array(ys)
    slope, intercept = np.polyfit(x, y, 1)
    pids = np.array(who)
    uniq = {p: i for i, p in enumerate(dict.fromkeys(who))}
    inv = np.array([uniq[p] for p in who])
    k = len(uniq)
    rng = np.random.default_rng(seed)
    slopes = []
    members = [np.where(inv == i)[0] for i in range(k)]
    for _ in range(reps):
        pick = rng.integers(0, k, size=k)
        idx = np.concatenate([members[j] for j in pick])
        xx, yy = x[idx], y[idx]
        slopes.append(np.polyfit(xx, yy, 1)[0] if xx.std() > 0 else 0.0)
    bins = [("none", x == 0), ("1-20 min absent", (x > 0) & (x <= 20)), ("21-40", (x > 20) & (x <= 40)), ("41+", x > 40)]
    return {"n": len(xs), "players": k, "slope_per_absent_minute": float(slope), "slope_lo": float(np.percentile(slopes, 2.5)),
            "slope_hi": float(np.percentile(slopes, 97.5)), "intercept": float(intercept),
            "by_bin": [{"bin": name, "n": int(m.sum()), "mean_minutes_change": float(y[m].mean()) if m.any() else None} for name, m in bins]}


def format_stage_b(abs_summary: dict, mech: dict, examples: List[dict]) -> str:
    L = [f"ABSENCES: {abs_summary['player_games']} player-games across {abs_summary['teams']} teams; "
         f"{abs_summary['with_any_absent']} had at least one absent rotation teammate, {abs_summary['with_absent_starter']} an absent starter."]
    for e in examples:
        L.append(f"  e.g. {e['player']} on {e['date']}: absent {', '.join(e['absent'])} ({e['absent_min']:.0f} normal minutes)")
    L.append("")
    L.append("MECHANISM: does an absent rotation teammate change the others' minutes (vs each player's own earlier average)?")
    if mech.get("n", 0) < 50 or "slope_per_absent_minute" not in mech:
        L.append(f"  not enough rows ({mech.get('n', 0)})")
    else:
        L.append(f"  {mech['n']} player-games from {mech['players']} players. Slope: {mech['slope_per_absent_minute']:+.3f} minutes per absent teammate-minute "
                 f"[95% {mech['slope_lo']:+.3f}, {mech['slope_hi']:+.3f}] (resampling players)")
        L.append(f"  i.e. about {10 * mech['slope_per_absent_minute']:+.1f} minutes for each 10 normal minutes of teammates who sat")
        for b in mech["by_bin"]:
            mc = "n/a" if b["mean_minutes_change"] is None else f"{b['mean_minutes_change']:+.2f}"
            L.append(f"    {b['bin']:18} n {b['n']:>5}  mean minutes vs own earlier average {mc}")
    L.append("")
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
    finally:
        await hw.aclose()
    print("Deriving absences, simulating and fitting...")
    abs_map, abs_summary = absence_features(players, info.get("teams") or {})
    mech = minutes_mechanism(players, abs_map, reps=args.reps)
    ranked = sorted(abs_map.items(), key=lambda kv: -kv[1]["absent_min"])[:3]
    name_of = {pid: n for pid, (n, _l) in players.items()}
    date_of = {(pid, g["game_id"]): g["date"] for pid, (_n, logs) in players.items() for g in logs if g.get("game_id")}
    examples = [{"player": name_of[k[0]], "date": date_of.get(k), "absent": v["absent_names"], "absent_min": v["absent_min"]} for k, v in ranked]
    records = run_backtest(players, params)
    rows, join = attach_board(records, boards)
    join["board_rows"] = binfo["board_rows"]
    missing = 0
    for r in rows:
        a = abs_map.get((r["player_id"], r["game_id"]))
        if a is None:
            missing += 1
            continue
        r["absent_min"], r["absent_starters"] = a["absent_min"], a["absent_starters"]
    if len(rows) < 400:
        print(f"Only {len(rows)} usable rows after joining boards; not enough to test.")
        return 1
    res = analyze(build_features(rows), l2=args.l2, reps=args.reps, models=MODELS_B)
    print()
    print(format_stage_b(abs_summary, mech, examples))
    print(f"(rows with no absence information treated as none absent: {missing})")
    print()
    print(format_report(info, join, res))
    out_dir = Path("evals/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest-stage-b-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"date": date, "season": season, "params": params.__dict__, "absences": abs_summary, "mechanism": mech,
                                "examples": examples, "join": join, "result": res}, indent=1, default=str))
    print(f"\nSaved to {path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="US Eastern date whose boards pick the players (default today)")
    ap.add_argument("--draws", type=int, default=4000)
    ap.add_argument("--l2", type=float, default=5.0)
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--max-players", type=int, default=None)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
