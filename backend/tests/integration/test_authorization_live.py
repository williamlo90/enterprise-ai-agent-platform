"""Explicit opt-in for the dedicated AUTH-03 database fixture."""

import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.environ.get("RUN_AUTHORIZATION_TESTS")!="1",reason="Dedicated AUTH-03 database check not requested")
def test_authorization_contract_on_real_restricted_postgres():
    root=Path(__file__).resolve().parents[3]
    result=subprocess.run([sys.executable,"-m","enterprise_ai_platform.authorization_check","--live","--env-file",str(root/".env")],
                          cwd=root,capture_output=True,text=True,timeout=900)
    assert result.returncode==0,"Authorization live verification failed; inspect sanitized CLI output only."
    assert '"disposable_cleanup": "completed"' in result.stdout
    assert '"SEC-ACCESS-094"' in result.stdout
    assert '"deactivate_revoke_reactivate_does_not_restore_grant"' in result.stdout
