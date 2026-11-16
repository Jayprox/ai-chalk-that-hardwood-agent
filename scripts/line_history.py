"""Line history: score a player's past games against ONE threshold (the stored line) and look at what separated the overs from the unders.

Descriptive and read-only. It reads Hardwood (game logs and defensive rankings) and one prop row from data/props.db. It never touches the
prospective ledger, the model, or the signal rules, and it must not be used on, or alongside, counted prospective data.

    python -m scripts.line_history --player "LeBron James" --market pts                    # threshold = stored board prop (see the report)
    python -m scripts.line_history --player-id <uuid> --market pts --line 17.5 --position F --board-date 2026-11-15   # supplied by hand

What the numbers are, and are not (the report repeats these)
  * Scoring old games against one threshold is HISTORICAL PRODUCTION CONTEXT. It is not historical sportsbook betting performance: the
    sportsbook never offered this number for those games.
  * The threshold is the stored board prop's line, from the stored board date printed in the report. The latest stored prop is not
    necessarily today's. When --line is given, the threshold is labelled as supplied manually.
  * Games on the source board date and later are excluded (their results are not history for that line). DNP games are not in the log and are
    never zero-scored; a game missing any stat the market needs is skipped; a genuine 0 is a valid value; pushes are counted separately.
  * Opponent defensive rank is RETROSPECTIVE: for finished seasons it is the end-of-season rank, which contains information from games after
    the one being looked at. For the current season it is the rank to date (as of the run), not a final rank. Neither is the rank on the night.
  * Minutes are what he actually played: retrospective context, not something known before tip-off.
  * The position group is the current one applied to older seasons: an approximation.
  * The older-half / newer-half columns are an exploratory consistency look at the same games. They are not independent confirmation.
  * Seasons are walked backwards from the current one and stop at the first season with no games (default: the current season plus up to 3
    earlier ones, regular season only).

Outputs go to data/research/ (outside git): a per-game CSV (each row carries the threshold and its source board date), a markdown report, and a
JSON provenance file. Raw Hardwood responses are cached there too (finished seasons only).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import current_season, load_settings, now_et  # noqa: E402
from app.simulation import MARKET_STATS, is_played  # noqa: E402

OUT = ROOT / "data" / "research"
CACHE = OUT / "cache"
SMALL_N = 8          # splits below this are flagged "small"


def prev_season(s: str) -> str:
    a = int(s[:4]) - 1
    return f"{a}-{(a + 1) % 100:02d}"


def _cur() -> str:
    return current_season(now_et())


def _cache_get(key: str) -> Any:
    p = CACHE / (key.replace("/", "_") + ".json")
    return json.loads(p.read_text()) if p.exists() else None


def _cache_put(key: str, val: Any) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / (key.replace("/", "_") + ".json")).write_text(json.dumps(val))


async def season_log(hw: Any, player_id: str, season: str) -> List[dict]:
    key = f"log_{player_id}_{season}"
    hit = _cache_get(key) if season != _cur() else None     # the current season keeps changing: never cache it
    if hit is not None:
        return hit
    res = await hw.post("/query", {"entity": "player", "id": player_id, "scope": "game_log", "season": season, "season_type": "regular"})
    data = res.get("data") or []
    if season != _cur():
        _cache_put(key, data)
    return data


async def collect_logs(hw: Any, player_id: str, n_seasons: int) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    s = _cur()
    for _ in range(n_seasons):
        log = await season_log(hw, player_id, s)
        if not log:
            if out:                      # an empty season after real ones = the career ended: stop
                break
            s = prev_season(s)           # an empty CURRENT season (before the opener) is not the end
            continue
        out[s] = log
        s = prev_season(s)
    return out


async def def_rank_table(hw: Any, opponents: List[str], position: str, market: str, season: str) -> Dict[str, Optional[int]]:
    """End-of-season (or current, for the current season) rank of each opponent against `position` for `market`."""
    teams = await hw.teams()
    ids = {t.get("abbreviation"): t.get("id") for t in teams}
    out: Dict[str, Optional[int]] = {}
    for abbr in opponents:
        tid = ids.get(abbr)
        if tid is None:
            out[abbr] = None
            continue
        key = f"rank_{tid}_{season}"
        data = _cache_get(key) if season != _cur() else None
        if data is None:
            try:
                params: Dict[str, Any] = {"team_id": tid}
                if season != _cur():
                    params["season"] = season
                data = (await hw.get("/rankings/matchups", params)).get("data") or {}
            except Exception:  # noqa: BLE001
                out[abbr] = None
                continue
            if season != _cur():
                _cache_put(key, data)
        try:
            v = ((data.get(position) or [{}])[0].get("rank") or {}).get(market)
            out[abbr] = int(v) if v is not None else None
        except (TypeError, ValueError, IndexError):
            out[abbr] = None
    return out


def build_rows(logs: Dict[str, List[dict]], market: str, line: float, ranks: Dict[str, Dict[str, Optional[int]]], before: Optional[str] = None) -> List[dict]:
    stats = MARKET_STATS[market]
    rows: List[dict] = []
    for season, log in logs.items():
        games = sorted((g for g in log if g.get("date")), key=lambda g: g["date"])
        prev: Optional[date] = None
        for g in games:
            if before and g["date"] >= before:      # the board date itself and later: its result is not history yet
                continue
            if not is_played(g):
                continue
            d = datetime.strptime(g["date"], "%Y-%m-%d").date()
            rest = (d - prev).days - 1 if prev else None
            prev = d
            vals = [g.get(k) for k in stats]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in vals):
                continue
            v = float(sum(vals))
            rows.append({"season": season, "date": g["date"], "opponent": g.get("opponent"), "venue": g.get("venue"),
                         "minutes": g.get("minutes"), "rest_days": rest, "value": v,
                         "result": "push" if v == line else ("over" if v > line else "under"),
                         "def_rank": ranks.get(season, {}).get(g.get("opponent")),
                         "def_rank_basis": "to-date" if season == _cur() else "end-of-season"})
    rows.sort(key=lambda r: r["date"])
    return rows


def _tier_def(r: dict) -> Optional[str]:
    k = r["def_rank"]
    return None if k is None else ("tough (1-10)" if k <= 10 else "middle (11-20)" if k <= 20 else "soft (21-30)")


def _tier_rest(r: dict) -> Optional[str]:
    k = r["rest_days"]
    return None if k is None else ("back-to-back" if k == 0 else "1 day" if k == 1 else "2+ days")


def stat_line(rows: List[dict]) -> dict:
    n = len(rows)
    o = sum(1 for r in rows if r["result"] == "over")
    u = sum(1 for r in rows if r["result"] == "under")
    dec = o + u
    return {"n": n, "over": o, "under": u, "push": n - o - u, "over_pct": (100 * o / dec) if dec else None, "avg": (sum(r["value"] for r in rows) / n) if n else None}


def _fmt(s: dict) -> str:
    pct = f"{s['over_pct']:.0f}% over" if s["over_pct"] is not None else ""
    push = f"{s['push']} push" if s.get("push") else ""
    extra = ", ".join(x for x in (pct, push) if x)
    return f"{s['over']}-{s['under']}" + (f" ({extra})" if extra else "")


def split_table(rows: List[dict], key) -> List[dict]:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        k = key(r)
        if k is not None:
            groups[k].append(r)
    mid = len(rows) // 2
    older, newer = {id(r) for r in rows[:mid]}, {id(r) for r in rows[mid:]}
    out = []
    for k, rs in groups.items():
        s = stat_line(rs)
        so = stat_line([r for r in rs if id(r) in older])
        sn = stat_line([r for r in rs if id(r) in newer])
        out.append({"split": k, **s, "older": _fmt(so) if so["n"] else "-", "newer": _fmt(sn) if sn["n"] else "-"})
    return out


def minutes_tiers(rows: List[dict]):
    mins = sorted(r["minutes"] for r in rows if isinstance(r["minutes"], (int, float)))
    if len(mins) < 9:
        return lambda r: None
    lo, hi = mins[len(mins) // 3], mins[2 * len(mins) // 3]
    return lambda r: None if not isinstance(r["minutes"], (int, float)) else (f"low (<{lo:g} min)" if r["minutes"] < lo else f"high ({hi:g}+ min)" if r["minutes"] >= hi else f"middle ({lo:g}-{hi:g} min)")


CONTEXT_NOTES = [
    "Scoring old games against one threshold is historical production context. It is not historical sportsbook betting performance.",
    "Opponent ranks are retrospective: end-of-season ranks for finished seasons contain information from games after the one being looked at; "
    "current-season ranks are to-date as of the run, not final.",
    "Minutes are what he actually played, which is retrospective context and not a pregame feature.",
    "The position group is the current one applied to older seasons, an approximation.",
    "The older-half / newer-half columns are an exploratory consistency look at the same games, not independent confirmation.",
    "Rest days count the gap since the last game he PLAYED, so they run high after any missed games.",
]


def provenance_lines(prov: dict) -> List[str]:
    src = ("supplied manually (not read from stored data)" if prov["threshold_source"] == "manual"
           else "selected from the stored board: the latest stored prop for this player and market, which is not necessarily today's")
    return [f"- Threshold: {prov['threshold']:g} ({prov['market']}), {src}.",
            f"- Source board date: {prov['source_board_date']}" + (" (this date and later games are excluded)." if prov.get("excluded_from") else "."),
            f"- Position group: {prov['position']} ({'supplied manually' if prov['position_source'] == 'manual' else 'from the stored matchup'}).",
            f"- Report generated / Hardwood data retrieved: {prov['generated_at']} (finished-season data may be served from the local cache)."]


def report(name: str, prov: dict, rows: List[dict], logs: Dict[str, List[dict]]) -> str:
    market, line, position = prov["market"], prov["threshold"], prov["position"]
    L: List[str] = [f"# Line history: {name}, {market}, threshold {line:g} (position group {position})", "",
                    "Descriptive research, not part of the prospective experiment. Games are scored against one threshold; this is production context, "
                    "not betting performance.", "", "## Provenance", ""] + provenance_lines(prov) + [""]
    if not rows:
        L += ["## Result", "", f"No eligible games: after excluding the source board date ({prov['source_board_date']}) and later, DNPs and games missing a "
              "required statistic, nothing remains to score. No splits were computed.", "", "## Reading notes", ""] + [f"- {n}" for n in CONTEXT_NOTES]
        return "\n".join(L) + "\n"
    allstat = stat_line(rows)
    ranked = sum(1 for r in rows if r["def_rank"] is not None)
    L += [f"Games scored: {allstat['n']} across seasons {', '.join(logs)}; record vs the threshold {_fmt(allstat)}; average {allstat['avg']:.1f}.",
          f"Opponent defensive rank is available for {ranked} of {allstat['n']} games (end-of-season for finished seasons, to-date for the current one).", ""]
    by_season = split_table(rows, lambda r: r["season"])
    L += ["## By season", "", "| Season | Games | vs threshold | Avg |", "|---|---|---|---|"]
    for s_ in sorted(by_season, key=lambda x: x["split"], reverse=True):
        L.append(f"| {s_['split']} | {s_['n']} | {_fmt(s_)} | {s_['avg']:.1f} |")
    n_splits = 0
    sections = [("Home / away", lambda r: r["venue"]), ("Opponent defense vs this position (retrospective rank)", _tier_def),
                ("Minutes played (actual, retrospective)", minutes_tiers(rows)), ("Days since last played game", _tier_rest)]
    head = ["| Split | Games | vs threshold | Older half | Newer half | Avg |", "|---|---|---|---|---|---|"]
    for title, key in sections:
        tbl = split_table(rows, key)
        n_splits += len(tbl)
        L += ["", f"## {title}", ""] + head
        if not tbl:
            L.append("| (not enough games to split) |  |  |  |  |  |")
        for s_ in sorted(tbl, key=lambda x: x["split"]):
            flag = " (small sample)" if s_["n"] < SMALL_N else ""
            L.append(f"| {s_['split']} | {s_['n']}{flag} | {_fmt(s_)} | {s_['older']} | {s_['newer']} | {s_['avg']:.1f} |")
    opp = [x for x in split_table(rows, lambda r: r["opponent"]) if x["n"] >= 3]
    n_splits += len(opp)
    L += ["", "## By opponent (3+ games)", ""] + head
    if not opp:
        L.append("| (no opponent with 3+ games) |  |  |  |  |  |")
    for s_ in sorted(opp, key=lambda x: (-x["n"], x["split"])):
        flag = " (small sample)" if s_["n"] < SMALL_N else ""
        L.append(f"| {s_['split']} | {s_['n']}{flag} | {_fmt(s_)} | {s_['older']} | {s_['newer']} | {s_['avg']:.1f} |")
    L += ["", "## Reading notes", ""] + [f"- {n}" for n in CONTEXT_NOTES] + [
          f"- This report shows {n_splits} splits. With that many, a few will look strong by chance; an over rate far from 50% on a few games means little.",
          "- The threshold is a single number applied to every season. Role and scoring level change over time, so check minutes and the by-season table before "
          "reading anything into an old game.",
          "- Any idea worth keeping has to be written down first and tested on games not yet looked at."]
    return "\n".join(L) + "\n"


def lookup_prop(player: Optional[str], player_id: Optional[str], market: str) -> Optional[dict]:
    db = ROOT / "data" / "props.db"
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    if player_id:
        r = c.execute("SELECT * FROM props_current WHERE player_id=? AND market=? ORDER BY date_et DESC LIMIT 1", (player_id, market)).fetchone()
    else:
        r = c.execute("SELECT * FROM props_current WHERE name=? AND market=? ORDER BY date_et DESC LIMIT 1", (player, market)).fetchone()
    return dict(r) if r else None


def build_provenance(a: argparse.Namespace, prop: Optional[dict], market: str, now: Optional[datetime] = None) -> Optional[dict]:
    """Where the threshold, board date and position came from. None when something needed is missing."""
    now = now or datetime.now(timezone.utc)
    manual = a.line is not None
    if manual:
        threshold, board = float(a.line), a.board_date or now_et().strftime("%Y-%m-%d")
    elif prop and prop.get("line") is not None:
        threshold, board = float(prop["line"]), prop["date_et"]
    else:
        return None
    position, psrc = a.position, "manual"
    if not position and prop and prop.get("extra"):
        position, psrc = (json.loads(prop["extra"]).get("matchup") or {}).get("position"), "stored"
    if not position:
        return None
    return {"market": market, "threshold": threshold, "threshold_source": "manual" if manual else "stored",
            "source_board_date": board, "excluded_from": board, "position": position, "position_source": psrc,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


async def main_async(a: argparse.Namespace) -> int:
    if a.market not in MARKET_STATS:
        print(f"unknown market {a.market}; choose from {sorted(MARKET_STATS)}")
        return 2
    prop = lookup_prop(a.player, a.player_id, a.market)
    pid = a.player_id or (prop or {}).get("player_id")
    name = a.player or (prop or {}).get("name") or pid
    prov = build_provenance(a, prop, a.market)
    if not pid or prov is None:
        print("need a player on the stored board (or pass --player-id, --line and --position)")
        return 2
    from app.hardwood import HardwoodClient
    s = load_settings()
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    try:
        logs = await collect_logs(hw, pid, a.seasons)
        if not logs:
            print("no games found for this player")
            return 1
        opponents = sorted({g.get("opponent") for log in logs.values() for g in log if g.get("opponent")})
        ranks = {season: await def_rank_table(hw, opponents, prov["position"], a.market, season) for season in logs}
    finally:
        await hw.aclose()
    rows = build_rows(logs, a.market, prov["threshold"], ranks, prov["source_board_date"])
    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"{(name or pid).replace(' ', '_')}_{a.market}"
    fields = ["season", "date", "opponent", "venue", "minutes", "rest_days", "value", "result", "def_rank", "def_rank_basis", "threshold", "source_board_date"]
    with open(OUT / f"{stem}_games.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "threshold": prov["threshold"], "source_board_date": prov["source_board_date"]})
    (OUT / f"{stem}_provenance.json").write_text(json.dumps({**prov, "player": name, "player_id": pid, "seasons": list(logs), "games_scored": len(rows),
                                                              "notes": CONTEXT_NOTES}, indent=2))
    md = report(str(name), prov, rows, logs)
    (OUT / f"{stem}_report.md").write_text(md)
    print(md)
    print(f"[wrote {OUT / (stem + '_report.md')}, {stem}_games.csv and {stem}_provenance.json]")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--player", help="exact name as on the stored board")
    p.add_argument("--player-id")
    p.add_argument("--market", required=True)
    p.add_argument("--line", type=float, help="threshold to use; if omitted, the stored board prop's line is used and labelled as such")
    p.add_argument("--board-date", help="with --line: the date the threshold belongs to (YYYY-MM-DD); that date and later games are excluded. Default: today (ET)")
    p.add_argument("--position", help="G, F or C; defaults to the stored matchup position")
    p.add_argument("--seasons", type=int, default=4, help="current season plus earlier ones; stops at the first season with no games")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
