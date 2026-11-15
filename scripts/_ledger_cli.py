"""Shared helpers for the ledger scripts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def settings():
    from app.config import load_settings
    return load_settings()


def open_ledger(s):
    from app.ledger import Ledger
    return Ledger(s.ledger_path, s.ledger_mirror_dir)


def env_secrets():
    return [os.environ.get(k, "") for k in ("HARDWOOD_API_KEY", "ANTHROPIC_API_KEY", "ODDS_API_KEY")]


def safe_write(path: Path, text: str) -> None:
    """Write a report only if it contains no credential-looking text."""
    from app.provenance import secrets_in
    bad = secrets_in(text, env_secrets())
    if bad:
        raise SystemExit("refusing to write a file that contains credential-like text: " + ", ".join(bad))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def launchd_plist(label: str, python: str, script: str, workdir: str, log: str, *, every_sec=None, at=None, args=()) -> str:
    """A launchd job for one of the ledger scripts. `at` = (hour, minute) in the Mac's local time for a daily job; `every_sec` for an interval job."""
    when = (f"<key>StartInterval</key><integer>{every_sec}</integer>" if every_sec
            else f"<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{at[0]}</integer><key>Minute</key><integer>{at[1]}</integer></dict>")
    argv = "".join(f"<string>{x}</string>" for x in (python, script, *args))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{argv}</array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  {when}
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def notify(title: str, text: str) -> None:
    """macOS notification (no-op elsewhere). Callers pass only status text, never outcomes or credentials."""
    import shutil
    import subprocess
    if shutil.which("osascript"):
        safe = text.replace('"', "'")[:200]
        try:
            subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'], timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
