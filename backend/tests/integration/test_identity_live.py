"""Actual restricted PostgreSQL with controlled signed OIDC; opt-in only."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.environ.get("RUN_IDENTITY_TESTS")!="1",reason="Dedicated AUTH-02 database check not requested")
def test_identity_contract_on_real_restricted_postgres():
    root=Path(__file__).resolve().parents[3]
    result=subprocess.run([sys.executable,"-m","enterprise_ai_platform.identity_check","--live","--env-file",str(root/".env"),
                           "--fixture",str(root/"backend/tests/contracts/identity_protocol_cases.json")],
                          cwd=root,capture_output=True,text=True,timeout=900)
    assert result.returncode==0,"Identity live verification failed; inspect sanitized CLI output only."
    assert '"disposable_cleanup": "completed"' in result.stdout
    assert '"SEC-06-086"' in result.stdout
