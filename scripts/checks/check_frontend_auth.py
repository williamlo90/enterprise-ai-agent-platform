"""Opt-in FE-BASE-03/AUTH-01 real HTTPS/API/browser check, disposable restricted PostgreSQL only."""
import argparse
import asyncio
import datetime
import ipaddress
import json
import logging
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import uvicorn
from psycopg import sql
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from enterprise_ai_platform.authorization_check import Harness, require, CheckFailure
from enterprise_ai_platform.composition import ApplicationServices, database_loop_factory
from enterprise_ai_platform.config import Settings, DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, direct_connection, parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role, drop_probe_schema
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.identity.service import IdentityService, digest

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'local/fe-auth-01'


async def browser_check(h, data):
    tls = OUT / ('tls-' + uuid4().hex)
    tls.mkdir(parents=True)
    key_path, cert_path = tls / 'key.pem', tls / 'cert.pem'
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(32)
    origin = 'https://127.0.0.1:' + str(listener.getsockname()[1])
    server = task = process = None
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'FE auth owned loopback fixture')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        identity = IdentityService(SimpleNamespace(public_origin=origin), PostgresIdentityRepository(h.database), None, None)
        settings = Settings(env='test', host='127.0.0.1', port=8000).model_copy(update={'browser_context_enabled': True})
        app = create_app(settings, services=ApplicationServices(health=SimpleNamespace(), identity=identity))
        async def page():
            return FileResponse(ROOT / 'frontend/dist/index.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        for path in ('/', '/chat', '/knowledge', '/access', '/appearance', '/tenants'):
            app.add_api_route(path, page, methods=['GET'], include_in_schema=False)
        app.mount('/assets', StaticFiles(directory=str(ROOT / 'frontend/dist/assets')))
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=listener.getsockname()[1], ssl_keyfile=str(key_path), ssl_certfile=str(cert_path), access_log=False, log_level='critical', proxy_headers=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    raise CheckFailure('owned_server_start')
                await asyncio.sleep(.05)
        node = ROOT / ('local/frontend-runtime/node-v' + (ROOT / 'frontend/.node-version').read_text().strip() + '-win-x64/node.exe')
        process = subprocess.Popen([str(node), str(ROOT / 'scripts/checks/frontend-auth-browser.mjs')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=0x08000000 if sys.platform == 'win32' else 0)
        stdout, _stderr = await asyncio.to_thread(process.communicate, input=json.dumps({'origin': origin, **data}).encode(), timeout=110)
        # Child emits only bounded status/check names. Credentials are never written to evidence.
        (OUT / 'browser-status.log').write_bytes(stdout)
        require(process.returncode == 0, 'owned_browser_checks')
        proof = json.loads((OUT / 'browser-proof.json').read_text())
        require(proof['passed'] and proof['owned_browser_closed'], 'browser_proof')
        return proof
    finally:
        if process is not None and process.poll() is None:
            if sys.platform == 'win32':
                await asyncio.to_thread(subprocess.run, ['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
            else:
                process.kill()
            await asyncio.to_thread(process.wait)
        if server:
            server.should_exit = True
        if task:
            await asyncio.wait_for(task, timeout=10)
        listener.close()
        require(tls.resolve().parent == OUT.resolve(), 'owned_tls_path')
        for path in (key_path, cert_path):
            if path.exists():
                path.unlink()
        tls.rmdir()


async def verify(direct, base):
    schema = 'db03_probe_' + uuid4().hex
    extension_schema = 'eai_vector_probe_' + uuid4().hex
    owned = extension_owned = False
    database = None
    before = await migrate(direct)
    try:
        async with direct_connection(direct) as admin:
            if not (await (await admin.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()):
                await admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
                await admin.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
                extension_owned = True
        await migrate(direct, schema=schema, upgrade=True)
        owned = True
        target = await provision_role(direct, base, schema=schema, role_name='eai_runtime_' + schema.rsplit('_', 1)[1])
        database = RuntimeDatabase(target, schema=schema)
        await database.open()
        async with direct_connection(direct, timeout=300) as admin:
            h = Harness(admin, database, schema)
            person, a, b, unassigned, admin_person = [uuid4() for _ in range(5)]
            await h.member(a, person, grants=(RoleGrant('employee', 'tenant', str(a)), RoleGrant('knowledge_manager', 'tenant', str(a))))
            member_b = await h.member(b, person, grants=(RoleGrant('knowledge_manager', 'tenant', str(b)),))
            await h.member(a, admin_person, grants=(RoleGrant('tenant_admin', 'tenant', str(a)),))
            for tenant, name in ((a, 'Fiktif Alpha'), (b, 'Fiktif Beta')):
                await h.execute('UPDATE {s}.tenants SET name=%s WHERE id=%s', (name, tenant))
            token_a, _ = await h.session(a, person)
            token_admin, _ = await h.session(a, admin_person)
            await h.execute('INSERT INTO {s}.principals(id,issuer,subject) VALUES(%s,%s,%s)', (unassigned, h.issuer, 'unassigned-' + unassigned.hex))
            async def empty_session(principal):
                token = secrets.token_urlsafe(32)
                await h.execute("INSERT INTO {s}.sessions(id,principal_id,token_hash,csrf_hash,expires_at) VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP+interval '8 hours')", (uuid4(), principal, digest('session', token), digest('csrf', secrets.token_urlsafe(32))))
                return token
            no_tenant = await empty_session(person)
            no_membership = await empty_session(unassigned)
            print('Disposable identity fixtures ready; starting one HTTPS browser', flush=True)
            browser = await browser_check(h, dict(a=token_a, admin=token_admin, noTenant=no_tenant, noMembership=no_membership, memberB=str(member_b)))
        require(await migrate(direct) == before, 'platform_unchanged')
        return dict(passed=True, browser_checks=len(browser['checks']), platform_revision=before[0], platform_schema_modified=False,
                    provider_login='not_tested_controlled_session_cookies', database='real_restricted_disposable', cleanup='completed')
    finally:
        if database:
            await database.close()
        if owned:
            await drop_probe_schema(direct, schema)
        if extension_owned:
            async with direct_connection(direct) as admin:
                row = await (await admin.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
                require(row == (extension_schema,), 'owned_extension')
                await admin.execute('DROP EXTENSION vector')
                await admin.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('Explicit --live required; only disposable fixtures are changed.')
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        settings = load_settings(Path('.env'), require_file=True, settings_type=DatabaseProbeSettings)
        base = parse_target(settings.database_url.get_secret_value())
        direct = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
        verify_same_database(base, direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result = runner.run(verify(direct, base))
        (OUT / 'database-proof.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        print(json.dumps(result))
    except Exception as error:
        print('Frontend auth verification failed: ' + (str(error) if isinstance(error, CheckFailure) else type(error).__name__))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
