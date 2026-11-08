import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.environ.get('RUN_JOB_TESTS')!='1',reason='JOB-01 disposable database check not requested')
def test_durable_job_races_fencing_and_tenant_fairness():
    root=Path(__file__).resolve().parents[3]
    result=subprocess.run([sys.executable,'-m','enterprise_ai_platform.job_check','--live','--env-file',str(root/'.env')],
                          cwd=root,capture_output=True,text=True,timeout=600)
    assert result.returncode==0,'Job verification failed; inspect sanitized CLI output only.'
    proof=json.loads((root/'local/job01/jobs-live.json').read_text())
    assert proof['passed'] and proof['check_count']>=50
    assert proof['real_postgres_restricted_runtime'] and proof['platform_schema_modified'] is False
    assert proof['disposable_cleanup']=='completed'
