"""Build the freeze manifest, then (after you have committed it) record the freeze. Does not start anything by itself.

    python scripts/freeze.py --build                       # writes docs/freeze/manifest.json and prints its hash
    python scripts/freeze.py --record --commit <hash>      # appends the freeze record; refuses unless every precondition holds

Preconditions for --record: the working tree is clean at the commit you name; the manifest on disk equals a fresh rebuild; the dry run
passes (>= 2 slate dates, coverage at the dry-run thresholds, integrity ok, no records after tip-off); no freeze exists yet.
first_counted_date = the next ET calendar date after the moment of recording (never backdated). Records from the dry run, and every game
examined before, are not counted (their freeze_id stays empty).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from _ledger_cli import ROOT, open_ledger, safe_write, settings

from app import protocol as P
from app.config import ET
from app.coverage import dry_run_acceptance
from app.ledger import canon, sha256_text, ts
from app.provenance import CORE_FILES, fingerprint, git_info, sha256_file

DOCS = ["docs/PROSPECTIVE_PROTOCOL.md", "docs/PROSPECTIVE_OPERATIONS.md", "docs/handoff_docs/historical_validation_plan.md"]
TESTS = ["tests/test_ledger.py", "tests/test_record.py", "tests/test_settle.py", "tests/test_coverage.py", "tests/test_analysis.py", "tests/test_freeze_flow.py", "tests/ledger_helpers.py"]
MANIFEST = ROOT / "docs" / "freeze" / "manifest.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build() -> dict:
    fp = fingerprint()
    return {"protocol": P.PROTOCOL_VERSION, "constants": {k: (list(v) if isinstance(v, tuple) else v) for k, v in P.constants().items()},
            "fingerprint": {"sim_version": fp["sim_version"], "fit_sha256": fp["fit_sha256"], "params": fp["params"], "slope": fp["slope"],
                            "code_sha": fp["code_sha"], "files": fp["files"]},
            "docs": {f: sha256_file(ROOT / f) for f in DOCS}, "tests": {f: sha256_file(ROOT / f) for f in TESTS}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--dry-from", help="first full slate date (ET, YYYY-MM-DD) of the dry run; earlier games are not judged")
    ap.add_argument("--dry-to", help="last ET date (inclusive) of the fixed two-slate acceptance cohort; must be given with --dry-from")
    ap.add_argument("--commit", help="the git commit that contains docs/freeze/manifest.json")
    a = ap.parse_args()
    if a.build == a.record:
        ap.error("choose exactly one of --build and --record")
    man = build()
    text = json.dumps(man, indent=2, sort_keys=True)
    if a.build:
        safe_write(MANIFEST, text + "\n")
        print(f"wrote {MANIFEST}\nmanifest sha256 {sha256_text(canon(man))}\nCommit it with git, then run --record --commit <hash>.")
        missing = [k for k, v in {**man['fingerprint']['files'], **man['docs'], **man['tests']}.items() if v is None]
        if missing:
            print("WARNING: missing files:", ", ".join(missing))
            return 1
        return 0

    problems = []
    gi = git_info(paths=sorted(set(CORE_FILES) | set(DOCS) | set(TESTS) | {"docs/freeze/manifest.json"}))
    if not a.commit:
        problems.append("--commit is required")
    if not a.dry_from:
        problems.append("--dry-from is required (the first full slate date of the dry run)")
    if not a.dry_to:
        problems.append("--dry-to is required (the last date of the fixed two-slate acceptance cohort)")
    if a.commit and gi["commit"] != a.commit:
        problems.append(f"HEAD is {str(gi['commit'])[:12]}, not the commit you named")
    if gi["clean"] is not True:
        problems.append("a file hashed in the manifest is untracked or has uncommitted changes (git status on those paths)")
    on_disk = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else None
    if on_disk != json.loads(text):
        problems.append("docs/freeze/manifest.json does not match the current code (run --build and commit again)")
    ledger = open_ledger(settings())
    try:
        if ledger.latest_freeze():
            problems.append("a freeze record already exists; a changed test needs a new ledger")
        now = _now()
        acc = dry_run_acceptance(ledger, a.dry_from, now, a.dry_to) if a.dry_from else {"final": {"reasons": []}}
        problems += acc["final"]["reasons"]
        if problems:
            print("NOT FROZEN:")
            for p in problems:
                print("  -", p)
            return 1
        first = (now.astimezone(ET) + timedelta(days=1)).strftime("%Y-%m-%d")
        fid = ledger.add_freeze(ts(now), first, man, gi["commit"], gi["clean"])
        print(f"freeze {fid} recorded at {ts(now)}; first counted date {first}; manifest sha256 {sha256_text(canon(man))}")
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    sys.exit(main())
