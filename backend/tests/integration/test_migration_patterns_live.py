import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.environ.get("RUN_FEATURE_MIGRATION_TESTS")!="1",reason="DB-04 disposable database check not requested")
def test_filled_feature_rollout_and_failure_recovery():
    root=Path(__file__).resolve().parents[3]
    result=subprocess.run([sys.executable,"-m","enterprise_ai_platform.migration_pattern_check","--live","--env-file",str(root/".env")],
                          cwd=root,capture_output=True,text=True,timeout=600)
    assert result.returncode==0,"Feature migration verification failed; inspect sanitized CLI output only."
    assert '"check_count": 52' in result.stdout
    assert '"platform_schema_modified": false' in result.stdout
    assert '"disposable_cleanup": "completed"' in result.stdout
