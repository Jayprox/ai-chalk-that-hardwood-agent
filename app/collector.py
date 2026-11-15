"""One-shot collector: pull all ten Chalk That prop markets for a date, score them, and store the snapshot.

    python -m app.collector [--date YYYY-MM-DD] [--db path] [--top 10]

Read-only against Hardwood (11 calls: /games plus one /props per market). No model calls, no cost.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.signal import line_move, signal
from app.store import Store

MARKETS = ["pts", "ast", "reb", "fg3m", "pa", "pr", "ra", "pra", "stl", "blk"]
ACTIVE_WINDOW = timedelta(hours=3)  # the 30-minute clock starts this long before tip-off
FINAL_STATUSES = {"final", "completed", "complete", "closed", "finished"}
LIVE_STATUSES = {"live", "in_progress", "inprogress", "in progress", "started"}


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def game_state(status: Any, tipoff_utc: Any, now: datetime, window: timedelta = ACTIVE_WINDOW) -> str:
    """upcoming | active (inside the pre-tip window) | tipped (line closed, game on) | final."""
    s = str(status or "").strip().lower()
    if s in FINAL_STATUSES:
        return "final"
    tip = _parse_utc(tipoff_utc)
    if s in LIVE_STATUSES or (tip is not None and now >= tip):
        return "tipped"
    if tip is not None and now >= tip - window:
        return "active"
    return "upcoming"


def _team(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        return v.get("abbreviation") or v.get("abbr") or v.get("name")
    return v if isinstance(v, str) else None


def normalize_game(g: dict, date_et: str, now: datetime, window: timedelta = ACTIVE_WINDOW) -> Optional[dict]:
    gid = g.get("id") or g.get("game_id")
    if not gid:
        return None
    return {"game_id": str(gid), "date_et": date_et, "home": _team(g.get("home")), "away": _team(g.get("away")),
            "tipoff_utc": g.get("tipoff_utc"), "status": g.get("status"), "state": game_state(g.get("status"), g.get("tipoff_utc"), now, window),
            "props_open_at": g.get("props_open_at"), "props_close_at": g.get("props_close_at"),
            "home_score": g.get("home_score"), "away_score": g.get("away_score")}


def normalize_prop(r: dict, market: str, date_et: str) -> Optional[dict]:
    if not r.get("player_id") or not r.get("game_id"):
        return None
    sig = signal(r.get("over_price"), r.get("under_price"))
    return {"player_id": str(r["player_id"]), "game_id": str(r["game_id"]), "market": market, "date_et": date_et,
            "name": r.get("name"), "team": r.get("team"), "opponent": r.get("opponent"), "venue": r.get("venue"),
            "line": r.get("line"), "open_line": r.get("open_line"), "over_price": r.get("over_price"), "under_price": r.get("under_price"),
            "snapshot_kind": r.get("snapshot"), "p_over": sig["p_over"], "side": sig["side"], "strength": sig["strength"],
            "line_move": line_move(r.get("line"), r.get("open_line")), "actual": r.get("actual"), "result": r.get("result"),
            "extra": json.dumps({k: r.get(k) for k in ("last10", "season", "last_season", "matchup")}, default=str)}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def collect_once(hw: Any, store: Store, date_et: str, now: Optional[datetime] = None, concurrency: int = 5,
                       window: timedelta = ACTIVE_WINDOW) -> dict:
    now = now or datetime.now(timezone.utc)
    started = _iso(now)
    errors: List[str] = []
    sem = asyncio.Semaphore(concurrency)

    async def board(market: str):
        async with sem:
            return market, await hw.get("/props", {"market": market, "date": date_et})

    async def games():
        async with sem:
            return await hw.get("/games", {"date": date_et})

    gres, *bres = await asyncio.gather(games(), *(board(m) for m in MARKETS), return_exceptions=True)

    game_rows: List[dict] = []
    if isinstance(gres, BaseException):
        errors.append(f"games: {type(gres).__name__}")
    else:
        game_rows = [x for x in (normalize_game(g, date_et, now, window) for g in (gres.get("data") or [])) if x]
    store.upsert_games(game_rows, started)

    per_market: Dict[str, int] = {}
    failed: List[str] = []
    props: List[dict] = []
    skipped = 0
    for market, res in zip(MARKETS, bres):
        if isinstance(res, BaseException):
            failed.append(market)
            errors.append(f"{market}: {type(res).__name__}")
            continue
        rows = res[1].get("data") or []
        per_market[market] = len(rows)
        for r in rows:
            p = normalize_prop(r, market, date_et)
            if p is None:
                skipped += 1
            else:
                props.append(p)

    stats = store.upsert_props(props, started)
    deactivated = 0
    for market, n in per_market.items():
        if n > 0:  # an empty board is not evidence that everyone was removed
            deactivated += store.deactivate_missing(date_et, market, started)

    warnings: List[str] = []
    if per_market and not any(per_market.values()):
        warnings.append("every market came back empty: lines may not be posted yet, try again in an hour")
    elif any(n == 0 for n in per_market.values()):
        warnings.append("empty markets: " + ", ".join(m for m, n in per_market.items() if n == 0))
    ok = bool(per_market) and len(failed) < len(MARKETS)
    summary = {"date": date_et, "ok": ok, "markets": per_market, "failed": failed, "rows": len(props), "inserted": stats["inserted"],
               "changed": stats["changed"], "skipped_no_ids": skipped, "deactivated": deactivated, "games": len(game_rows),
               "game_states": {s: sum(1 for g in game_rows if g["state"] == s) for s in ("upcoming", "active", "tipped", "final")},
               "errors": errors, "warnings": warnings}
    store.record_run(started, _iso(datetime.now(timezone.utc)), date_et, len(props), stats["changed"], skipped, ok, summary)
    return summary


def format_summary(s: dict, store: Store, top: int = 10) -> str:
    L = [f"Collected {s['date']}: {s['rows']} props ({s['inserted']} new, {s['changed']} changed, {s['deactivated']} no longer listed)."]
    L.append("  per market: " + ", ".join(f"{m} {n}" for m, n in s["markets"].items()))
    gs = s["game_states"]
    L.append(f"  {s['games']} games: {gs['upcoming']} upcoming, {gs['active']} in window, {gs['tipped']} tipped, {gs['final']} final")
    if s["skipped_no_ids"]:
        L.append(f"  skipped {s['skipped_no_ids']} rows without player_id or game_id")
    for w in s["warnings"]:
        L.append(f"  WARNING: {w}")
    for e in s["errors"]:
        L.append(f"  ERROR: {e}")
    games = {g["game_id"]: g for g in store.games_for(s["date"])}
    L.append("")
    L.append("GAMES")
    for g in games.values():
        n = len(store.props(s["date"], game_id=g["game_id"]))
        L.append(f"  {g['away']} @ {g['home']}  {g['tipoff_utc']}  {g['state']:8} {n} props")
    L.append("")
    L.append(f"TOP {top} BY WITHIN-MARKET RANK (percentile of market tilt, unvalidated; raw tilt in points)")
    best = [p for p in store.props(s["date"]) if p["pct"] is not None][:top]
    for p in best:
        mv = "" if p["line_move"] in (None, 0) else f" (moved {p['line_move']:+g})"
        L.append(f"  p{p['pct']:5.1f}  tilt {p['strength']:4.1f}  {p['name']} {p['market']} {p['side']} {p['line']}{mv}  [{p['team']} vs {p['opponent']}]")
    return "\n".join(L)


async def main_async(args: argparse.Namespace) -> int:
    from app.config import load_settings, now_et
    from app.hardwood import HardwoodClient

    s = load_settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    date = args.date or now_et().strftime("%Y-%m-%d")
    store = Store(args.db or s.db_path)
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    try:
        summary = await collect_once(hw, store, date)
    finally:
        await hw.aclose()
    print(format_summary(summary, store, args.top))
    print(f"\nSaved to {store.path}")
    store.close()
    return 0 if summary["ok"] else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", help="US Eastern date of the board (default today)")
    ap.add_argument("--db", help="SQLite path (default DB_PATH or data/props.db)")
    ap.add_argument("--top", type=int, default=10)
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
