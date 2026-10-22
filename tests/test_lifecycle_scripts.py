"""Two pytest entry points; the PowerShell harness reports its own inner counts.

The identity suite uses real PS5 helper functions, native argument parsing, JSON
files, and cross-process mutexes. CIM/listener/Mongo observations are fixtures;
the running application, real network ports, and database are never accessed.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "lifecycle_identity.ps1"
HELPER = ROOT / "scripts" / "lifecycle.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell 5 is required")


def run_harness(mode, fixture_root=None):
    arguments = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(HARNESS),
                 "-HelperPath", str(HELPER), "-Mode", mode]
    if fixture_root is not None:
        arguments.extend(["-FixtureRoot", str(fixture_root)])
    return subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def test_powershell_lifecycle_script_syntax():
    result = run_harness("Parse")
    assert result.returncode == 0, result.stdout + result.stderr
    count = re.search(r"LIFECYCLE_AST_OK (\d+)", result.stdout)
    assert count and int(count.group(1)) >= 5, result.stdout


def test_powershell_lifecycle_identity_boundaries_and_mutex():
    # The space and Chinese characters exercise native process-argument parsing.
    with tempfile.TemporaryDirectory(prefix="启停 身份-", dir=ROOT) as fixture_root:
        result = run_harness("Suite", fixture_root)
    assert result.returncode == 0, result.stdout + result.stderr
    summary_line = next((line for line in result.stdout.splitlines() if line.startswith("LIFECYCLE_RESULT ")), None)
    assert summary_line is not None, result.stdout
    summary = json.loads(summary_line.removeprefix("LIFECYCLE_RESULT "))
    assert summary["failed"] == 0, summary
    assert summary["scenarios"] >= 45 and summary["assertions"] >= 65, summary
    print(f"PowerShell helper: {summary['scenarios']} inner scenarios, {summary['assertions']} assertions, 0 failed")
