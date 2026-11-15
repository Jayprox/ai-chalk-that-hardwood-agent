"""Model and code provenance saved with every ledger record and hashed into the freeze manifest. No credentials, ever."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional

from app.config import ROOT

CORE_FILES = ["app/simulation.py", "app/minutes_model.py", "app/sim.py", "app/signal.py", "app/calibration.py", "app/baseline.py",
              "app/protocol.py", "app/ledger.py", "app/record.py", "app/settle.py", "app/coverage.py", "app/analysis.py", "app/provenance.py",
              "app/scheduler.py", "app/config.py", "app/collector.py", "app/sim_fit.json", "scripts/_ledger_cli.py", "scripts/ledger_check.py",
              "scripts/ledger_daily.py", "scripts/settle.py", "scripts/freeze.py", "scripts/prospective_analysis.py"]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def git_info(root: Path = ROOT, paths=None) -> Dict[str, object]:
    """Commit hash and whether the tree is clean. With `paths`, "clean" means those paths (the files hashed in the manifest) are tracked and
    unmodified; unrelated untracked or modified files elsewhere do not matter. Falls back to None when git is unavailable (never raises)."""
    def run(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=10)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--", *paths) if paths else run("status", "--porcelain")
    return {"commit": commit, "clean": (status == "") if status is not None else None}


def params_dict(params=None) -> dict:
    from app.simulation import Params
    d = asdict(params if params is not None else Params())
    d.pop("minutes", None)                     # the fitted minutes model is covered by the sim_fit.json hash
    return d


def fingerprint(root: Path = ROOT) -> dict:
    """Everything that determines a forecast: model version, fit file hash, parameters, shrink slope, code hashes, git state."""
    from app import calibration
    from app.sim import sim_version
    files = {f: sha256_file(root / f) for f in CORE_FILES}
    params = params_dict()
    code_sha = sha256_bytes(json.dumps(files, sort_keys=True).encode())
    return {"sim_version": sim_version(), "fit_sha256": files.get("app/sim_fit.json"), "params": params, "slope": calibration.SLOPE,
            "files": files, "code_sha": code_sha, "git": git_info(root)}


def secrets_in(text: str, env_values=()) -> list:
    """Names of credential-looking things found in text (keys from the environment, ctnba_ / sk-ant / long token patterns). Never returns the value."""
    import re
    found = []
    for i, v in enumerate(env_values):
        if v and len(v) >= 8 and v in text:
            found.append(f"environment value #{i + 1}")
    for name, pat in (("hardwood key", r"ctnba_[A-Za-z0-9]{8,}"), ("anthropic key", r"sk-ant-[A-Za-z0-9_-]{8,}"),
                      ("api-key header", r"(?i)x-api-key[\"': =]+[A-Za-z0-9_-]{12,}"), ("odds api key", r"(?i)apikey=[A-Za-z0-9]{12,}")):
        if re.search(pat, text):
            found.append(name)
    return found
