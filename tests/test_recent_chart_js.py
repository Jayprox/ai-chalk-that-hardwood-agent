import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_recent_chart_client_logic():
    """The browser-side chart cache and drawing (static/recent_chart.js) behave correctly when a prop's line moves."""
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "recent_chart_check.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
