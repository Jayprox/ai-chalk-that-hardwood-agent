"""Read-only check of which prop markets Hardwood serves and what each row carries.

Run from the project folder with the venv active:
    python -m scripts.market_check            # today's US Eastern date
    python -m scripts.market_check 2026-11-13 # a specific date

Makes about 13 GET calls to Hardwood (no model calls, no cost). Prints counts only, never the API key.
"""
from __future__ import annotations

import asyncio
import json
import sys

from app.config import load_settings, now_et
from app.hardwood import MARKETS, HardwoodClient

CHALK_THAT = ["pts", "ast", "reb", "fg3m", "pa", "pr", "ra", "pra", "stl", "blk"]


async def main(date: str) -> None:
    s = load_settings()
    hw = HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    try:
        print(f"Date {date}. Markets the app knows: {', '.join(MARKETS)}")
        print(f"{'market':7} {'rows':>5} {'w/matchup':>10} {'w/L10':>6} {'w/season':>9}  label / notes")
        total = 0
        sample = {}
        for m in CHALK_THAT + [x for x in MARKETS if x not in CHALK_THAT]:
            try:
                res = await hw.get("/props", {"market": m, "date": date})
            except Exception as e:  # report and keep going
                print(f"{m:7} ERROR {type(e).__name__}: {str(e)[:90]}")
                continue
            rows = res.get("data") or []
            meta = res.get("meta") or {}
            wm = sum(1 for r in rows if r.get("matchup"))
            wl = sum(1 for r in rows if (r.get("last10") or {}).get("games"))
            ws = sum(1 for r in rows if (r.get("season") or {}).get("games"))
            if m in CHALK_THAT:
                total += len(rows)
            print(f"{m:7} {len(rows):>5} {wm:>10} {wl:>6} {ws:>9}  {meta.get('market_label')} | {str(meta.get('notes'))[:70]}")
            if rows:
                sample[m] = rows[0]
            if m == "pts":
                print("\nFULL NOTES (pts):")
                for n in meta.get("notes") or []:
                    print("  -", n)
                print("GAMES (pts): away at home tipoff_utc status lines close_pulled")
                for g in res.get("games") or []:
                    print("  ", g.get("away"), "at", g.get("home"), g.get("tipoff_utc"), g.get("status"), g.get("lines"), g.get("close_pulled"))
                if rows:
                    r0 = rows[0]
                    print("ROW line fields:", {k: r0.get(k) for k in ("line", "open_line", "snapshot", "over_price", "under_price")})
                print()
        print(f"Total rows across the ten Chalk That markets: {total}")
        for m in ("pra", "pr", "stl", "blk", "fg3m"):
            if m in sample:
                print(f"\nSample {m} matchup: {json.dumps(sample[m].get('matchup'))[:260]}")
        # What the matchup_ranks endpoint covers
        try:
            team = None
            for r in sample.values():
                team = r.get("opponent")
                if team:
                    break
            if team:
                t = await hw.resolve_team(team)
                res = await hw.get("/rankings/matchups", {"team_id": t["id"]})
                data = res.get("data") or {}
                for pos, rows in data.items():
                    row = rows[0] if rows else {}
                    allowed = row.get("allowed")
                    keys = sorted(allowed) if isinstance(allowed, dict) else sorted(row)
                    print(f"\n/rankings/matchups ({team}, {pos}) stat keys: {', '.join(keys)}")
                    break
        except Exception as e:
            print(f"\n/rankings/matchups check failed: {type(e).__name__}: {str(e)[:90]}")
    finally:
        await hw.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else now_et().strftime("%Y-%m-%d")))
