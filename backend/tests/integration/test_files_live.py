import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.environ.get('RUN_FILE_TESTS')!='1',reason='FILE-01 disposable PostgreSQL check not requested')
def test_file_http_recovery_and_inspection_generation_contract():
    root=Path(__file__).resolve().parents[3]
    result=subprocess.run([sys.executable,'-m','enterprise_ai_platform.file_check','--live','--env-file',str(root/'.env')],cwd=root,capture_output=True,text=True,timeout=600)
    assert result.returncode==0,'File verification failed; inspect sanitized CLI diagnostics.'
    proof=json.loads((root/'local/file01/contract-live.json').read_text(encoding='utf-8'))
    assert proof['passed'] and proof['check_count']>=44 and proof['disposable_cleanup']=='completed'
    assert proof['platform_schema_modified'] is False and proof['storage_provider']=='controlled_memory_fixture_not_live_S3'
