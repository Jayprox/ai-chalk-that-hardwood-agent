"""Daily housekeeping (run once each morning ET): settle, audit the schedule against the manifest, write a report. Reads no outcomes.

    python scripts/ledger_daily.py

Audit: every date in the last 14 days is re-read from the Hardwood schedule. A game missing from the pre-outcome manifest is added and
flagged `registered_late` (it stays in the coverage denominator as uncovered). Nothing is dropped.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import argparse

from _ledger_cli import ROOT, launchd_plist, notify, open_ledger, safe_write, settings

from app.config import ET, current_season, now_et
from app.coverage import coverage_report, health_report, integrity_report
from app.hardwood import HardwoodClient
from app.ledger import ts
from app.record import norm_game
from app.settle import settle_once


async def audit_schedule(hw, ledger, now: datetime, days: int = 14, since: str = "") -> int:
    """Re-read the schedule of the last `days` days, never earlier than `since` (the first full day after the recorder started)."""
    added = 0
    for k in range(days + 1):
        d = (now.astimezone(ET) - timedelta(days=k)).strftime("%Y-%m-%d")
        if d < since:
            continue
        try:
            res = await hw.get("/games", {"date": d})
        except Exception:  # noqa: BLE001
            continue
        for g in res.get("data") or []:
            ng = norm_game(g, d)
            if ng and ledger.register_game(ng, ts(datetime.now(timezone.utc))):
                added += 1
    return added


async def run() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--print-launchd", action="store_true", help="print a launchd job that runs this at 09:30 Mac-local time with --notify")
    a = ap.parse_args()
    if a.print_launchd:
        print(launchd_plist("com.chalkthat.ledger-daily", sys.executable, str(ROOT / "scripts" / "ledger_daily.py"), str(ROOT),
                            str(ROOT / "logs" / "ledger_daily.log"), at=(9, 30), args=("--notify",)), end="")
        return 0
    s = settings()
    if not s.hardwood_api_key:
        print("HARDWOOD_API_KEY is not set in .env")
        return 2
    ledger, hw = open_ledger(s), HardwoodClient(s.hardwood_base_url, s.hardwood_api_key)
    try:
        now = datetime.now(timezone.utc)
        settle = await settle_once(hw, ledger, current_season(now_et()))
        first = ledger._one("SELECT MIN(started_at) AS t FROM ticks")["t"]
        since = ((datetime.fromisoformat(first.replace("Z", "+00:00")).astimezone(ET) + timedelta(days=1)).strftime("%Y-%m-%d")) if first else "9999-12-31"
        added = await audit_schedule(hw, ledger, now, since=since)
        scope = "counted" if ledger.latest_freeze() else "dry"
        cov = coverage_report(ledger, scope, now, dry_run=(scope != "counted"))
        health = health_report(ledger, now)
        integ = integrity_report(ledger, now, "all")
    finally:
        await hw.aclose()
        ledger.close()
    report = {"as_of": ts(now), "settle": settle, "late_registered_games_added": added, "coverage": cov, "health": health, "integrity": integ}
    path = ROOT / "data" / "ledger" / "reports" / f"daily-{now_et():%Y-%m-%d}.json"
    safe_write(path, json.dumps(report, indent=2, default=str))
    problems = cov["invalid_reasons"] + health["alerts"] + integ["problems"] + settle["errors"]
    print(f"daily report: {path}")
    print(f"expected {cov['expected_games']}, covered {cov['covered_games']}; late-registered added {added}; settle {settle['settled']} new, {settle['changed']} changed")
    for p in problems:
        print("ALERT:", p)
    if problems and a.notify:
        notify("Prospective ledger daily", f"{len(problems)} alert(s): " + "; ".join(problems)[:150])
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
