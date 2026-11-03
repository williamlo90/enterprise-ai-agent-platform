"""Actual restricted login, two tenants, RLS and pool cleanup; explicit opt-in."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Dedicated live database not requested")
def test_live_runtime_role_context_and_row_isolation():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, "-m", "enterprise_ai_platform.security_cli", "verify",
                             "--env-file", str(root / ".env")],
                            cwd=root, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, "Live security checks failed; inspect only sanitized CLI diagnostics."
    assert "Runtime isolation verification passed:" in result.stdout
    assert "disposable roles and schema removed." in result.stdout
    assert all(line == "Database driver event." for line in result.stderr.splitlines()), "Unexpected driver diagnostics."
