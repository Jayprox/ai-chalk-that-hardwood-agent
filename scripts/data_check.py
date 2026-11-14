"""Read-only check of what Hardwood data exists for a minutes-driven / game-level simulation and a backtest.

Run from the project folder with the venv active:
    python -m scripts.data_check            # today's US Eastern date
    python -m scripts.data_check 2026-11-13 # a specific date

About 20 GET/POST calls to Hardwood. No model calls, no cost. Never prints the API key.
Prints field NAMES and small counts, plus a few truncated sample values, never whole boards.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

from app.config import current_season, load_settings, now_et
from app.hardwood import HardwoodClient

SPREAD_HINT = re.compile(r"spread|total|odds|moneyline|\bml\b|handicap|over_under|line", re.I)


def short(x, n=240):
    s = json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + "..."


async def safe(label, coro):
    try:
        return await coro
    except Exception as e:  # report and carry on
        print(f"  [{label}] ERROR {type(e).__name__}: {str(e)[:120]}")
        return None


async def main(date: str) -> None:
    s = load_settings()
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    season = current_season(now_et())
    try:
        print(f"Date {date}, season {season}\n")

        # 1. Pick a player from today's board (highest points line)
        print("1) PLAYER GAME LOG (the raw fields, before the app trims them)")
        board = await safe("props", hw.get("/props", {"market": "pts", "date": date}))
        rows = (board or {}).get("data") or []
        rows = sorted(rows, key=lambda r: r.get("line") or 0, reverse=True)
        player_row = rows[0] if rows else None
        if not player_row:
            print("  no points props on this date; try another date")
        else:
            name = player_row["name"]
            team = player_row.get("team")
            print(f"  player: {name} ({team})")
            p = await safe("resolve", hw.resolve_player(name))
            if p:
                res = await safe("game_log", hw.post("/query", {"entity": "player", "id": p["id"], "scope": "game_log",
                                                                 "season": season, "season_type": "regular"}))
                logs = (res or {}).get("data") or []
                print(f"  games in this season's log: {len(logs)} (the app's tool shows at most 30)")
                if logs:
                    print(f"  ALL fields in a game-log row: {sorted(logs[0].keys())}")
                    mins = [g.get("minutes") for g in logs]
                    print(f"  minutes by game (newest first as returned): {mins[:20]}")
                    with_props = [g for g in logs if g.get("props")]
                    print(f"  rows with a non-empty 'props' field: {len(with_props)} of {len(logs)}")
                    if with_props:
                        print(f"  sample props field: {short(with_props[0]['props'], 400)}")
                    print(f"  started/rest fields sample: {short({k: logs[0].get(k) for k in ('started', 'rest_days', 'player_rest_days', 'player_b2b_night')})}")
                    print(f"  meta: {short((res or {}).get('meta'), 300)}")

        # 2. Past boards carry the result? (backtest data)
        print("\n2) PAST PROP BOARDS (do they include the actual result?)")
        meta = (board or {}).get("meta") or {}
        prev = meta.get("prev_date")
        seen = 0
        while prev and seen < 4:
            past = await safe("past props", hw.get("/props", {"market": "pts", "date": prev}))
            prow = (past or {}).get("data") or []
            done = [r for r in prow if r.get("result") is not None or r.get("actual") is not None]
            print(f"  {prev}: {len(prow)} points props, {len(done)} with actual/result"
                  + (f", sample: {short({k: prow[0].get(k) for k in ('name', 'line', 'actual', 'result', 'snapshot')})}" if prow else ""))
            prev = ((past or {}).get("meta") or {}).get("prev_date")
            seen += 1

        # 3. Game-level fields: any spread / total / odds?
        print("\n3) GAMES ENDPOINT (any spread, total or odds?)")
        g = await safe("games", hw.get("/games", {"date": date}))
        glist = (g or {}).get("data") or []
        print(f"  games: {len(glist)}")
        if glist:
            keys = sorted(glist[0].keys())
            print(f"  game fields: {keys}")
            hits = [k for k in keys if SPREAD_HINT.search(k)]
            print(f"  fields that look like betting data: {hits or 'none'}")
            for k in hits:
                print(f"    {k} = {short(glist[0].get(k), 160)}")
            gid = glist[0].get("id")
            box = await safe("box", hw.get(f"/games/{gid}")) if gid else None
            data = (box or {}).get("data") or {}
            print(f"  game-detail top-level fields: {sorted(data.keys())}")
            game = data.get("game") or {}
            print(f"  game record fields: {sorted(game.keys())}")
            hits2 = [k for k in game if SPREAD_HINT.search(k)]
            print(f"  betting-like fields in game record: {hits2 or 'none'}")
            teams = data.get("teams") or []
            if teams and teams[0].get("players"):
                pl = teams[0]["players"]
                print(f"  team record fields: {sorted(k for k in teams[0] if k != 'players')}")
                print(f"  box-score player fields: {sorted(pl[0].keys())}")
                dnp = [x.get('full_name') for x in pl if x.get('dnp')]
                print(f"  DNP players in team 0: {len(dnp)} (reasons: {sorted({str(x.get('dnp_reason')) for x in pl if x.get('dnp')})[:5]})")

        # 4. Usage and team ratings
        print("\n4) USAGE AND TEAM RATINGS")
        lb = await safe("usage leaderboard", hw.post("/query", {"entity": "player", "scope": "leaderboard", "stat": "usg_pct",
                                                                 "season": season, "season_type": "regular", "limit": 3}))
        ldata = (lb or {}).get("data") or []
        print(f"  usg_pct leaderboard rows: {len(ldata)}; fields: {sorted(ldata[0].keys()) if ldata else 'n/a'}")
        tr = await safe("team ratings", hw.get("/rankings/teams", {"season": season, "scope": "season", "season_type": "regular"}))
        tdata = (tr or {}).get("data") or []
        print(f"  team rating rows: {len(tdata)}; fields: {sorted(tdata[0].keys()) if tdata else 'n/a'}")
        print(f"  notes: {short((tr or {}).get('meta', {}).get('notes'), 300)}")
    finally:
        await hw.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else now_et().strftime("%Y-%m-%d")))
