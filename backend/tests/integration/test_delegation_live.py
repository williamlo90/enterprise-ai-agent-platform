import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.environ.get("RUN_DELEGATION_TESTS")!="1",reason="Dedicated AUTH-04 database check not requested")
def test_internal_delegation_on_real_postgres():
    root=Path(__file__).resolve().parents[3]
    result=subprocess.run([sys.executable,"-m","enterprise_ai_platform.delegation_check","--live","--env-file",str(root/".env")],
                          cwd=root,capture_output=True,text=True,timeout=900)
    assert result.returncode==0,"Delegation live verification failed; inspect sanitized CLI output only."
    assert '"SEC-DELEGATION-070"' in result.stdout
    assert '"logout_during_last_receipt_read_denied"' in result.stdout
    assert '"disposable_cleanup": "completed"' in result.stdout
