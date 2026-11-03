"""Real PostgreSQL checks, isolated from the application's platform schema."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Dedicated live database not requested")
def test_live_migrations_and_foundation_constraints():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, "-m", "enterprise_ai_platform.migration_cli", "verify",
                             "--env-file", str(root / ".env")],
                            cwd=root, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, "Live migration checks failed; only sanitized CLI diagnostics are available."
    assert "Migration verification passed:" in result.stdout
    assert "disposable schema removed." in result.stdout
    assert all(line == "Database driver event." for line in result.stderr.splitlines()), "Unexpected driver diagnostics."
