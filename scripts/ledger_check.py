"""Integrity, coverage and health report for the prospective ledger. Reads no outcomes.

    python scripts/ledger_check.py                  # all records: integrity + health + coverage (dry-run thresholds)
    python scripts/ledger_check.py --scope counted  # only records counted after the freeze (normal thresholds)
    python scripts/ledger_check.py --replay 20      # also recompute 20 saved forecasts from their saved inputs
    python scripts/ledger_check.py --json

Exit code 0 = everything passes, 1 = at least one problem or alert.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from _ledger_cli import ROOT, launchd_plist, notify, open_ledger, settings

from app.coverage import coverage_report, dry_run_acceptance, health_report, integrity_report, replay_sample


def verdict(problems, last_tick, rows, expected_games) -> str:
    """One line that never presents "nothing observed yet" as an evaluated success."""
    if problems:
        return "PROBLEMS:"
    if last_tick is None and not rows and not expected_games:
        return "NOT YET VERIFIED: no observations yet (no recorder tick, no expected games, no records)"
    if not expected_games:
        return "NO FAILURES, BUT NOT YET EVALUATED: no game has tipped in this scope, so coverage is not judged"
    return "RECORDING HEALTH PASS (evaluated against the games that have tipped)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scope", choices=("dry", "counted", "all"), default="all")
    ap.add_argument("--from-date", help="evaluate only games on or after this ET date (the first full slate day of the dry run)")
    ap.add_argument("--to-date", help="with --from-date: last ET date of the fixed acceptance cohort (inclusive), e.g. the second full slate day")
    ap.add_argument("--stage", choices=("recording", "final"), help="with --from-date: judge the dry-run acceptance stage (recording = after two slate days; final = after they matured and settled)")
    ap.add_argument("--replay", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--notify", action="store_true", help="show a macOS notification when something is wrong")
    ap.add_argument("--print-launchd", action="store_true", help="print a launchd job that runs this check every 15 minutes with --notify")
    ap.add_argument("--require-data", action="store_true", help="also fail when no expected game has been evaluated yet")
    a = ap.parse_args()
    if a.print_launchd:
        print(launchd_plist("com.chalkthat.ledger-check", sys.executable, str(ROOT / "scripts" / "ledger_check.py"), str(ROOT),
                            str(ROOT / "logs" / "ledger_check.log"), every_sec=900, args=("--notify",)), end="")
        return 0
    ledger = open_ledger(settings())
    now = datetime.now(timezone.utc)
    if a.stage:
        if not a.from_date:
            ap.error("--stage needs --from-date")
        try:
            acc = dry_run_acceptance(ledger, a.from_date, now, a.to_date)
        finally:
            ledger.close()
        st = acc[a.stage]
        print(f"dry run cohort {a.from_date}..{a.to_date or '(open-ended)'}: slate dates with covered games {acc['slate_dates']}")
        cov = acc["coverage"]
        print(f"expected {cov['expected_games']}, covered {cov['covered_games']}, unmatured {len(cov['unmatured_games'])}, "
              f"matured contracts {cov['settlement']['matured_contracts']} {cov['settlement']['by_status']}")
        print(f"STAGE {a.stage.upper()}: " + ("PASS" if st["ok"] else "NOT YET / FAIL"))
        for r in st["reasons"]:
            print("  -", r)
        return 0 if st["ok"] else 1
    try:
        integ = integrity_report(ledger, now, a.scope, a.from_date, a.to_date)
        health = health_report(ledger, now)
        cov = coverage_report(ledger, a.scope, now, dry_run=(a.scope != "counted"), check_chain=False, from_date=a.from_date, to_date=a.to_date)
        rep = replay_sample(ledger, a.replay) if a.replay else None
    finally:
        ledger.close()
    out = {"integrity": integ, "health": health, "coverage": cov, "replay": rep}
    problems = integ["problems"] + health["alerts"] + cov["invalid_reasons"] + (["replay mismatches: " + ", ".join(rep["mismatches"])] if rep and not rep["ok"] else [])
    if a.require_data and not cov["expected_games"]:
        problems.append("no expected games have been evaluated yet")
    if a.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"records {integ['rows']}; chain head {integ['chain_head'][:12]}; records after tip-off: {integ['records_after_tip']}; model fingerprints: {integ['fingerprints']}")
        print(f"last tick {health['last_tick']} ({health['last_tick_age_min'] if health['last_tick_age_min'] is None else round(health['last_tick_age_min'], 1)} min ago)")
        print(f"games: expected {cov['expected_games']}, covered {cov['covered_games']}"
              + (f" ({cov['coverage_games']:.1%})" if cov['coverage_games'] is not None else "")
              + f"; contracts recorded {cov['recorded_contracts']} of {cov['observed_contracts']} observed"
              + (f" ({cov['coverage_contracts']:.1%})" if cov['coverage_contracts'] is not None else ""))
        print("uncovered reasons:", cov["uncovered_reasons"] or "none", "| rejections:", cov["rejections_by_reason"] or "none")
        print("settlement (status counts only):", cov["settlement"]["by_status"] or "nothing matured yet")
        if rep:
            print(f"replay: {rep['checked']} checked, {len(rep['mismatches'])} mismatches")
        print(verdict(problems, health["last_tick"], integ["rows"], cov["expected_games"]))
        print("(recording health only; dry-run acceptance is separate: --scope dry --from-date D1 --to-date D2 --stage recording|final)")
        for p in problems:
            print("  -", p)
    if problems and a.notify:
        notify("Prospective ledger", f"{len(problems)} problem(s): " + "; ".join(problems)[:150])
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
