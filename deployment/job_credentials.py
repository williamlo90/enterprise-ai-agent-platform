"""Generate private enrollment files only; operator executes SQL separately."""
import argparse
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tenants', type=Path, required=True)
    p.add_argument('--expires-at', required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    expiry = datetime.fromisoformat(a.expires_at)
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise SystemExit('Explicit future UTC expiry required.')
    tenants = json.loads(a.tenants.read_text(encoding='utf-8'))
    if type(tenants) is not list or not 1 <= len(tenants) <= 50 or len(set(tenants)) != len(tenants):
        raise SystemExit('Use 1–50 unique existing tenant UUIDs.')
    for tenant in tenants:
        if str(UUID(tenant)) != tenant:
            raise SystemExit('Invalid tenant UUID.')
    a.out.mkdir(parents=True, exist_ok=False)
    a.out.chmod(0o700)
    scheduler = secrets.token_urlsafe(32)
    mapping, rows = {}, []
    for tenant in [None] + tenants:
        key = scheduler if tenant is None else secrets.token_urlsafe(32)
        who = str(uuid4())
        digest = hashlib.sha256(('eai:job-caller:v1:' + key).encode()).hexdigest()
        if tenant is not None:
            mapping[who] = key
        rows.append(
            "INSERT INTO platform.job_callers(id,credential_hash,capability,tenant_id,expires_at) "
            "VALUES('%s','%s','%s',%s,'%s');" % (
                who, digest, 'scheduler' if tenant is None else 'executor',
                'NULL' if tenant is None else "'" + tenant + "'", expiry.isoformat()))
    files = {
        'enroll.sql': 'BEGIN;\n' + '\n'.join(rows) + '\nCOMMIT;\n',
        'worker.env': 'SCHEDULER_CREDENTIAL=' + scheduler + '\nEXECUTORS_JSON=' + json.dumps(mapping, separators=(',', ':')) + '\n',
    }
    for name, content in files.items():
        fd = os.open(a.out / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
    print('Private enrollment files written. Apply SQL as operator; merge worker.env. No credentials printed.')


if __name__ == '__main__':
    main()
