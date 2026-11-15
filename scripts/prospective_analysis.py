"""The single primary analysis, run only at the endpoint. Refuses to run early. Never run it to peek.

    python scripts/prospective_analysis.py              # runs once, stores the result in the ledger and data/ledger/analysis/
    python scripts/prospective_analysis.py --reproduce  # recomputes and checks it equals the stored result (no new record)

Preconditions: a freeze record exists; 300 covered games have matured (tip-off + 3 h + 48 h); the integrity and coverage rules pass; no
earlier analysis run is stored. Definitions: app/analysis.py and docs/PROSPECTIVE_PROTOCOL.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from _ledger_cli import ROOT, open_ledger, safe_write, settings

from app import protocol as P
from app.analysis import run_primary
from app.coverage import coverage_report, integrity_report
from app.ledger import canon, sha256_text, ts
from app.provenance import fingerprint
from app.settle import settled_rows


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reproduce", action="store_true")
    a = ap.parse_args()
    ledger = open_ledger(settings())
    try:
        now = _now()
        fr = ledger.latest_freeze()
        if not fr:
            print("No freeze record: nothing is counted yet.")
            return 2
        cov = coverage_report(ledger, "counted", now)
        integ = integrity_report(ledger, now, "counted")
        ep = cov["endpoint"]
        if not ep["ready"]:
            print(f"Endpoint not reached: {ep['covered_matured_games']} of {P.ENDPOINT_GAMES} covered games have matured. Not running.")
            return 2
        prior = ledger.analysis_runs()
        if prior and not a.reproduce:
            print("The primary analysis has already been run and stored. Use --reproduce to verify it.")
            return 2
        rows = settled_rows(ledger, ep["game_ids"])
        res = run_primary(rows, coverage_ok=cov["ok"], integrity_ok=integ["ok"], reps=P.BOOT_REPS, seed=P.BOOT_SEED)
        res["coverage_summary"] = {k: cov[k] for k in ("expected_games", "covered_games", "coverage_games", "coverage_contracts", "invalid_reasons")}
        games_sha = sha256_text(canon(ep["game_ids"]))
        code_sha = fingerprint()["code_sha"]
        if a.reproduce:
            if not prior:
                print("No stored analysis to reproduce.")
                return 2
            same = json.loads(prior[-1]["result_json"]) == json.loads(canon(res))
            print("reproduced: identical" if same else "MISMATCH with the stored result")
            return 0 if same else 1
        ledger.add_analysis_run(ts(now), fr["id"], games_sha, res, code_sha)
        safe_write(ROOT / "data" / "ledger" / "analysis" / f"primary-{now:%Y%m%dT%H%M%SZ}.json", json.dumps(res, indent=2))
        print(json.dumps({"track_a": res["track_a"].get("category"), "track_b": res["track_b"].get("category")}, indent=2))
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    sys.exit(main())
