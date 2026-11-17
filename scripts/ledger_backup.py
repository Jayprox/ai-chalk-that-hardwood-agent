"""Consistent backup of the ledger using SQLite's online backup facility (safe while the recorder is running).

    python scripts/ledger_backup.py            # writes data/ledger/backups/<UTC stamp>/ledger.db + mirror/, then verifies the copy

Never copy ledger.db / -wal / -shm files directly while the scheduler runs: the three files change independently and a plain copy can be
inconsistent. This script copies through sqlite3's backup API (a point-in-time snapshot), copies the JSONL mirror files, and runs the
ledger's own chain + mirror verification on the copy. Exit 0 only if the copy verifies. Keep backups off the internal disk too (an
external drive or cloud folder) by pointing --dest there. The folder is outside git (data/ is ignored).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from _ledger_cli import settings

from app.ledger import Ledger


def backup(db: Path, mirror: Path, dest_root: Path, stamp: str | None = None) -> dict:
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = dest_root / stamp
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    dest.mkdir(parents=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        out = sqlite3.connect(dest / "ledger.db")
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    if mirror.exists():
        shutil.copytree(mirror, dest / "mirror")
    else:
        (dest / "mirror").mkdir()
    L = Ledger(dest / "ledger.db", dest / "mirror")
    try:
        chain = L.verify_chain()
    finally:
        L.close()
    return {"dest": str(dest), "ok": bool(chain.get("ok")), "problems": chain.get("problems", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", help="backup root (default data/ledger/backups)")
    a = ap.parse_args()
    s = settings()
    dest = Path(a.dest) if a.dest else Path(s.ledger_path).parent / "backups"
    r = backup(Path(s.ledger_path), Path(s.ledger_mirror_dir), dest)
    print(("backup verified: " if r["ok"] else "BACKUP FAILED VERIFICATION: ") + r["dest"])
    for p in r["problems"][:10]:
        print("  -", p)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
