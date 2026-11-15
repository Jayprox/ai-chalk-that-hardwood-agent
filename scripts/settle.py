"""Settle recorded forecasts against final Hardwood results (separate table; ledger rows are never changed).

    python scripts/settle.py
"""
from __future__ import annotations

import asyncio
import sys

from _ledger_cli import open_ledger, settings

from app.config import current_season, now_et
from app.hardwood import HardwoodClient
from app.settle import settle_once


async def run() -> int:
    s = settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    ledger, hw = open_ledger(s), HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    try:
        out = await settle_once(hw, ledger, current_season(now_et()))
    finally:
        await hw.aclose()
        ledger.close()
    print(out)
    return 0 if not out["errors"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
