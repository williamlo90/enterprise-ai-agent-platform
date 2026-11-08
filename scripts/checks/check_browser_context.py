"""Explicit G-13 disposable PostgreSQL/API check. No provider/cloud/paid calls."""
import argparse
import asyncio
import json
import logging
import secrets
import socket
import datetime
import ipaddress
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from urllib.parse import parse_qs, urlsplit, urlencode
import httpx
from psycopg import sql
from enterprise_ai_platform.authorization_check import require, CheckFailure
from enterprise_ai_platform.knowledge_lifecycle_check import Harness as LifecycleHarness
from enterprise_ai_platform.knowledge_check import Extractor, source_hashes, write_json
from enterprise_ai_platform.conversation_check import Harness as ConversationHarness
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.identity.service import IdentityService, digest
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, direct_connection, parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate, revisions, revision_sql
from enterprise_ai_platform.infrastructure.database.roles import provision_role, drop_probe_schema
from enterprise_ai_platform.composition import ApplicationServices, database_loop_factory
from enterprise_ai_platform.config import Settings, DatabaseProbeSettings, load_settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.file_check import ORIGIN


class Harness(LifecycleHarness):
    create_conversation=ConversationHarness.create_conversation
    append=ConversationHarness.append
    def app(self, origin=ORIGIN, enabled=True):
        # Explicit fixture DI: production Settings validation is covered independently.
        identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
        from enterprise_ai_platform.modules.files.service import FilesService
        from enterprise_ai_platform.modules.knowledge.service import KnowledgeService
        services=ApplicationServices(health=SimpleNamespace(),identity=identity,
            files=FilesService(self.file_repo,self.store,origin),knowledge=KnowledgeService(self.repo,origin),
            conversations=ConversationService(self.conv_repo,origin,sources=self.conversations.sources))
        settings=Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':enabled})
        return create_app(settings,services=services)
    async def client(self):
        services=ApplicationServices(health=SimpleNamespace(),files=self.files,knowledge=self.knowledge,conversations=self.conversations)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)),base_url=ORIGIN)
    async def setup_context(self):
        self.conv_repo=PostgresConversationRepository(self.database,live_sources=True)
        self.conversations=ConversationService(self.conv_repo,ORIGIN)
        await self.setup_lifecycle()
        self.document_row=await self.document('Fictional travel claims policy: submit within seven days.')
        require((await self.mutate(self.document_row,'publish')).status_code==200,'fixture-publish')
        self.a=(await self.create_conversation(index=0)).json()['id']
        self.b=(await self.create_conversation(index=2)).json()['id']
        sent=(await self.append(self.a,index=0)).json();self.run_id=sent['run_id']
        self.file_id=str(await self.scalar('SELECT file_id FROM {s}.document_versions WHERE id=%s',(UUID(self.document_row['version_id']),)))
        self.alternate=await self.member(self.people[2]['tenant'],self.people[0]['person'],grants=(RoleGrant('employee','tenant',str(self.people[2]['tenant'])),))
    def cookie(self,index=0):return '__Host-eai-session='+self.people[index]['token']
    async def boot(self,client,index=0):
        response=await client.get('/api/v1/me',headers={'cookie':self.cookie(index),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'})
        require(response.status_code==200,'bootstrap-'+str(response.status_code))
        self.people[index]['csrf']=response.json()['csrf_token']
        return response.json()
    def bound_headers(self,binding,index=0):return {'cookie':self.cookie(index),'x-eai-expected-context':binding}
    async def protocol(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()),base_url=ORIGIN) as client:
            a=await self.boot(client);again=await self.boot(client);b=await self.boot(client,2)
            require(a['context_binding']==again['context_binding'] and a['csrf_token']!=again['csrf_token'],'csrf-independent-binding')
            require(a['context_binding']!=b['context_binding'],'actual-context-unique')
            self.binding=a['context_binding']
            left,right=await asyncio.gather(
                client.get('/api/v1/conversations',headers=self.bound_headers(self.binding)),
                client.get('/api/v1/conversations',headers=self.bound_headers(b['context_binding'],2)))
            require(left.status_code==right.status_code==200 and left.headers.get('x-eai-context-binding')==self.binding and right.headers.get('x-eai-context-binding')==b['context_binding'],'parallel-attestation-isolated')
            require(self.a in left.text and self.b not in left.text and self.b in right.text and self.a not in right.text,'parallel-buffer-isolated')
            self.passed('two_real_parallel_requests_keep_task_attestation_and_tenant_buffers_isolated')
            for path,index,binding in [('/api/v1/conversations',0,self.binding),('/api/v1/conversations',2,b['context_binding']),
                ('/api/v1/conversations/'+self.a+'/messages',0,self.binding),('/api/v1/runs/'+self.run_id,0,self.binding),
                ('/api/v1/runs/'+self.run_id+'/diagnostics',0,self.binding),('/api/v1/files/'+self.file_id+'/status',0,self.binding),
                ('/api/v1/files/'+self.file_id+'/content',0,self.binding),
                ('/api/v1/knowledge/documents/'+self.document_row['document_id']+'/versions/'+self.document_row['version_id']+'/status',0,self.binding),
                ('/api/v1/knowledge/search?q=claims&language=en',0,self.binding),
                ('/api/v1/knowledge/sources/'+self.document_row['version_id']+'/content',0,self.binding)]:
                response=await client.get(path,headers=self.bound_headers(binding,index))
                require(response.status_code==200,'rolled-route-'+str(path.split('/')[3])+':'+str(response.status_code))
                require(response.headers.get('x-eai-context-binding')==binding,'committed-response-binding')
            self.passed('all_present_private_read_families_attested_after_real_restricted_transactions')
            mismatch=await client.get('/api/v1/conversations',headers=self.bound_headers(self.binding,2))
            require(mismatch.status_code==409 and self.b not in mismatch.text and 'x-eai-context-binding' not in mismatch.headers,'aba-no-foreign-list')
            after=await self.boot(client)
            require(after['context_binding']==self.binding,'aba-pre-post-A-equal')
            self.passed('actual_cookie_B_read_rejected_with_A_expectation_even_when_pre_post_A_equal')
            for value in (None,'a'*63,'g'*64):
                headers={'cookie':self.cookie()}
                if value is not None:headers['x-eai-expected-context']=value
                require((await client.get('/api/v1/conversations',headers=headers)).status_code==400,'required-header')
            self.passed('enabled_required_header_missing_and_malformed_fail_closed')
            await self.file_barriers(client)
            await self.lock_expiry()
            await self.rotation(client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app(enabled=False)),base_url=ORIGIN) as legacy:
            require((await legacy.get('/api/v1/conversations',headers=self.bound_headers(self.binding))).status_code==503,'legacy-no-negotiated-success')
        self.passed('disabled_protocol_rejects_bound_client_without_legacy_fallback')
    async def file_barriers(self,client):
        async def revoked():await self.execute("UPDATE {s}.files SET deletion_state='deletion_pending' WHERE id=%s",(UUID(self.file_id),))
        self.store.read_hook=revoked
        try:
            response=await client.get('/api/v1/files/'+self.file_id+'/content',headers=self.bound_headers(self.binding))
            require(response.status_code==404 and 'Fictional travel' not in response.text,'post-storage-current-revoke')
        finally:
            self.store.read_hook=None
            await self.execute("UPDATE {s}.files SET deletion_state='active' WHERE id=%s",(UUID(self.file_id),))
        async def expired():await self.execute("WITH moment AS (SELECT clock_timestamp() AS t) UPDATE {s}.sessions SET authenticated_at=moment.t-interval '1 hour',expires_at=moment.t+interval '7 hours',last_seen_at=moment.t-interval '31 minutes' FROM moment WHERE token_hash=%s",(digest('session',self.people[0]['token']),))
        self.store.read_hook=expired
        try:
            response=await client.get('/api/v1/files/'+self.file_id+'/content',headers=self.bound_headers(self.binding))
            require(response.status_code==401 and 'Fictional travel' not in response.text,'post-storage-expiry')
        finally:
            self.store.read_hook=None
            await self.execute('UPDATE {s}.sessions SET last_seen_at=clock_timestamp() WHERE token_hash=%s',(digest('session',self.people[0]['token']),))
        self.passed('buffered_file_reference_cannot_attest_after_storage_revoke_or_expiry')

    async def lock_expiry(self):
        reached=asyncio.Event();state={};database=self.database;original=self.conv_repo
        class TracedDatabase:
            schema=database.schema
            @asynccontextmanager
            async def transaction(inner):
                async with database.transaction() as connection:
                    class Connection:
                        async def execute(inner,statement,*args,**kwargs):
                            if 'browser_context_guard' in statement.as_string() and not reached.is_set():
                                state['pid']=(await(await connection.execute('SELECT pg_backend_pid()')).fetchone())[0]
                                reached.set()
                            return await connection.execute(statement,*args,**kwargs)
                    yield Connection()
        self.conv_repo=PostgresConversationRepository(TracedDatabase())
        reader=None
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()),base_url=ORIGIN) as client:
                async with self.admin.transaction():
                    await self.execute('SELECT id FROM {s}.sessions WHERE token_hash=%s FOR UPDATE',(digest('session',self.people[0]['token']),))
                    reader=asyncio.create_task(client.get('/api/v1/conversations',headers=self.bound_headers(self.binding)))
                    await asyncio.wait_for(reached.wait(),timeout=2)
                    async with asyncio.timeout(1):
                        while not await self.scalar('SELECT cardinality(pg_blocking_pids(%s))>0',(state['pid'],)):
                            await asyncio.sleep(.02)
                    # Same row lock; the blocked guard must re-read clock after it acquires the row.
                    await self.execute("WITH moment AS (SELECT clock_timestamp() AS t) UPDATE {s}.sessions SET authenticated_at=moment.t-interval '1 hour',expires_at=moment.t+interval '7 hours',last_seen_at=moment.t-interval '31 minutes' FROM moment WHERE token_hash=%s",(digest('session',self.people[0]['token']),))
                response=await reader
                require(response.status_code==401 and self.a not in response.text,'expiry-after-real-row-lock-wait')
        finally:
            self.conv_repo=original
            if reader is not None and not reader.done():reader.cancel();await asyncio.gather(reader,return_exceptions=True)
            await self.execute('UPDATE {s}.sessions SET last_seen_at=clock_timestamp() WHERE token_hash=%s',(digest('session',self.people[0]['token']),))
        self.passed('real_session_row_lock_wait_then_expiry_denies_before_resource_read')

    async def rotation(self,client):
        before=await self.boot(client)
        response=await client.post('/api/v1/session/tenant',json={'membership_id':str(self.alternate)},headers=self.headers(0,'application/json'))
        require(response.status_code==200 and response.json()['context_binding']!=before['context_binding'],'switch-new-epoch')
        require((await client.get('/api/v1/conversations',headers=self.bound_headers(before['context_binding']))).status_code==401,'old-session-revoked')
        self.people[0]['token']=client.cookies.get('__Host-eai-session')
        await self.boot(client)
        response=await client.post('/api/v1/session/tenant',json={'membership_id':str(self.people[0]['member'])},headers=self.headers(0,'application/json'))
        require(response.status_code==200,'switch-back')
        self.people[0]['token']=client.cookies.get('__Host-eai-session')
        after=await self.boot(client)
        require(after['active_tenant_id']==before['active_tenant_id'] and after['context_binding']!=before['context_binding'],'A-new-epoch-not-reused')
        self.binding=after['context_binding']
        self.passed('actual_tenant_switch_A_B_A_revokes_old_tokens_and_never_reuses_epoch')
        # Real callback service + SQL lifecycle, controlled exchange port (not a Cognito login).
        from cryptography.fernet import Fernet
        from enterprise_ai_platform.integrations.identity.authentication import FernetVerifierProtector
        class Exchange:
            async def exchange(inner,*_args):return {'subject':'fictional-'+self.people[0]['person'].hex}
        provider=SimpleNamespace(public_origin=ORIGIN,issuer=self.issuer,client_id='g13-fixture',redirect_uri=ORIGIN+'/api/v1/auth/callback',authorization_endpoint='https://provider.invalid/authorize')
        identity=IdentityService(provider,PostgresIdentityRepository(self.database),Exchange(),FernetVerifierProtector(Fernet.generate_key().decode()))
        old,csrf=await self.session(self.people[0]['tenant'],self.people[0]['person'])
        index=len(self.people);self.people.append(dict(token=old,csrf=csrf))
        original=await self.boot(client,index)
        login=await identity.login('home',None,'127.0.0.1')
        state=parse_qs(urlsplit(login['location']).query)['state'][0]
        finished=await identity.callback(urlencode({'state':state,'code':'controlled-fixture'}).encode(),login['binding'],old,'127.0.0.1')
        self.people[index]['token']=finished['token']
        fresh=await self.boot(client,index)
        require(fresh['active_tenant_id'] is None and fresh['context_binding']!=original['context_binding'],'callback-new-epoch-no-tenant')
        denied=await client.get('/api/v1/conversations',headers=self.bound_headers(fresh['context_binding'],index))
        require(denied.status_code==403,'no-active-tenant-private-denied')
        require((await client.get('/api/v1/me',headers={'cookie':'__Host-eai-session='+old,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'})).status_code==401,'callback-old-token-revoked')
        self.passed('controlled_callback_real_SQL_rotates_epoch_and_no_tenant_bootstrap_is_consistent')


async def browser_check(h):
    """One owned TLS server and browser; credentials travel only over child stdin."""
    import uvicorn
    import subprocess
    from starlette.responses import HTMLResponse
    from starlette.staticfiles import StaticFiles
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    root=Path(__file__).resolve().parents[2];out=root/'local/g13'
    require((out/'browser-assets/browser-client.js').is_file(),'browser-bundle-ready')
    tls=out/('tls-'+uuid4().hex);tls.mkdir()
    key_path=tls/'key.pem';cert_path=tls/'cert.pem'
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(32)
    origin='https://127.0.0.1:'+str(listener.getsockname()[1]);server=None;server_task=None;process=None
    try:
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'G13 controlled localhost fixture')])
        now=datetime.datetime.now(datetime.timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        app=h.app(origin)
        @app.get('/__g13')
        async def page():return HTMLResponse('<!doctype html><html lang="en"><title>G13 controlled fixture</title><body>G13 controlled fixture</body></html>')
        app.mount('/__g13/assets',StaticFiles(directory=str(out/'browser-assets')))
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=listener.getsockname()[1],ssl_keyfile=str(key_path),ssl_certfile=str(cert_path),access_log=False,log_level='critical',proxy_headers=False))
        server_task=asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():raise CheckFailure('owned-server-start')
                await asyncio.sleep(.05)
        version=(root/'frontend/.node-version').read_text().strip()
        executable=root/('local/frontend-runtime/node-v'+version+'-win-x64/node.exe')
        process=subprocess.Popen([str(executable),str(root/'scripts/checks/browser_context/real-browser.mjs')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x08000000 if sys.platform=='win32' else 0)
        payload=json.dumps(dict(origin=origin,a=h.people[0]['token'],b=h.people[2]['token'],cidA=h.a,cidB=h.b,alternate=str(h.alternate))).encode()
        stdout,_stderr=await asyncio.to_thread(process.communicate,input=payload,timeout=60)
        (out/'real-browser.log').write_bytes(stdout)
        require(process.returncode==0,'real-browser-consumer')
        proof=json.loads((out/'real-browser-proof.json').read_text())
        require(proof.get('passed') and proof.get('owned_browser_closed'),'real-browser-proof')
        return proof
    finally:
        if process is not None and process.poll() is None:
            # Only the owned helper tree; never inspect/stop user browser processes.
            if sys.platform=='win32':
                await asyncio.to_thread(subprocess.run,['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)
            else:process.kill()
            await asyncio.to_thread(process.wait)
        if server is not None:server.should_exit=True
        if server_task is not None:await asyncio.wait_for(server_task,timeout=10)
        listener.close()
        # Exact files in an owned UUID directory, never a computed recursive deletion.
        require(tls.resolve().parent==out.resolve(),'owned-tls-path')
        for path in (key_path,cert_path):
            if path.exists():path.unlink()
        tls.rmdir()


async def verify(direct,base):
    schema='db03_probe_'+uuid4().hex;extension_schema='eai_vector_probe_'+uuid4().hex
    owned=False;owned_extension=False;database=None;stage='preflight'
    before=await migrate(direct);sources=source_hashes()
    try:
        async with direct_connection(direct) as connection:
            if not (await (await connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()):
                async with connection.transaction():
                    await connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
                    await connection.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
                owned_extension=True
            require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'owned-namespace')
        stage='migration14';await migrate(direct,schema=schema,upgrade=True,target_version=14);owned=True
        stage='migration15';await migrate(direct,schema=schema,upgrade=True)
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1])
        database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as admin:
            h=Harness(admin,database,schema,Extractor())
            stage='fixtures';await h.setup_context();print('G13 fixtures ready',flush=True)
            await admin.execute(sql.SQL('DROP FUNCTION {}.browser_context_guard(text,text)').format(sql.Identifier(schema)))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app()),base_url=ORIGIN) as client:
                missing=await client.get('/api/v1/me',headers={'cookie':h.cookie(),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'})
                require(missing.status_code==503 and 'csrf_token' not in missing.text,'missing-migration-fail-closed')
            h.passed('enabled_with_missing_0015_capability_does_not_emit_identity_or_binding')
            await admin.execute(revision_sql(revisions()[-1],schema));print('G13 revision15 capability restored',flush=True)
            stage='protocol';await h.protocol();print('G13 protocol checks completed',flush=True)
            stage='real_browser';browser=await browser_check(h);print('G13 HTTPS browser checks completed',flush=True)
            require(source_hashes()==sources,'source-stable')
            require(await migrate(direct)==before,'platform-unchanged')
            return dict(passed=True,checks=h.checks,check_count=len(h.checks),tested_revision=15,
                platform_revision=before[0],platform_schema_modified=False,provider_storage_parser_embedding='controlled_no_paid_or_cloud_calls',
                cleanup='completed',source_sha256=sources,browser_checks=len(browser['checks']))
    except Exception as error:
        # Keep diagnostics to status/type/SQLSTATE, never raw exception/connection/payload.
        cause=error;seen=set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause));print('G13 diagnostic '+type(cause).__name__+':status='+str(getattr(cause,'status',None))+':sqlstate='+str(getattr(cause,'sqlstate',None)),flush=True);cause=cause.__context__
        raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)
        if owned_extension:
            async with direct_connection(direct) as connection:
                found=await(await connection.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
                require(found==(extension_schema,),'owned-extension-cleanup')
                await connection.execute('DROP EXTENSION vector');await connection.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/g13/database-proof.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result)
        print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:
        print('G13 verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'))
        raise SystemExit(1) from None


if __name__=='__main__':main()
