"""Opt-in only. Dedicated test branch, internally loaded .env, no credential output."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Dedicated live database not requested")
def test_live_database_transaction_lifecycle():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-m", "enterprise_ai_platform.database_check",
         "--env-file", str(root / ".env"), "--live"],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    # Never echo captured provider output to pytest failure reports.
    assert result.returncode == 0, "Live database checks failed; inspect configuration locally."
    assert "Database checks passed" in result.stdout
    # Expected pool replacement warnings are allowed only in redacted form.
    assert all(line == "Database driver event." for line in result.stderr.splitlines()), "Unexpected driver diagnostics."
