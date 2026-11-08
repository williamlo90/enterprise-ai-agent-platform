"""FLOW01 actual leased worker and live service authority proof.

Only storage/parser and HTTPS ASGI transport are controlled external fixtures.
Workers, grants, eligibility, source HTTP writes, pins and ledger are actual.
"""
import argparse
import asyncio
import copy
from dataclasses import replace
from datetime import datetime,timedelta
import hashlib
import json
import logging
from pathlib import Path
import secrets
import sys
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID,uuid4
from zoneinfo import ZoneInfo
import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import sql
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from check_policy_eligibility import Harness as AuthorityHarness,SigningFixture,Receipts,ISSUER,AUDIENCE,CALLER
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform import composition as platform_composition
from enterprise_ai_platform import main as platform_main
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.integrations.reimbursement.receipt_access import ClaimReceiptReader
from enterprise_ai_platform.integrations.reimbursement.draft_sources import DraftSourceReader
from enterprise_ai_platform.integrations.reimbursement.decision_source import DecisionSourceReader
from enterprise_ai_platform.infrastructure.database.actions import PostgresHumanReceiptReader
from enterprise_ai_platform.modules.jobs.ports import Lease
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository
from enterprise_ai_platform.modules.flows.domain import FlowFailure
from enterprise_ai_platform.modules.delegation.domain import request_hash
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier
from enterprise_ai_platform.infrastructure.database.delegation import PostgresDelegationRepository
from enterprise_ai_platform.modules.delegation.service import DelegationService
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.modules.identity.service import digest as identity_digest
from enterprise_ai_platform.modules.files.domain import FileFailure
from check_claims import Target,schema_and_role,migrate as reference_migrate,cleanup as reference_cleanup
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase as ReferenceDatabase,direct_connection as reference_connection
from reimbursement_service.modules.policies.domain import Package,canonical_body,encoded
from reimbursement_service.modules.claims.domain import ClaimPermit
from reimbursement_service.modules.claims.ports import ClaimFailure
from reimbursement_service.integrations.security.terminal import Signer
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.integrations.platform.receipt_pins import ReceiptPinsClient
from reimbursement_service.composition import claims_service
from reimbursement_service import composition as reference_composition
from reimbursement_service.main import create_app as reference_app
from reimbursement_service.config import Settings as ReferenceSettings

ROOT=Path(__file__).resolve().parents[2]
TRACE=[]


class ObservedTransport(httpx.AsyncBaseTransport):
    def __init__(self,app):self.inner=httpx.ASGITransport(app=app)
    async def handle_async_request(self,request):
        response=await self.inner.handle_async_request(request)
        path=request.url.path
        label='eligibility' if path.endswith('/eligibility') else 'actor' if path.endswith('/verify') else 'pins' if '/receipt-pins' in path else 'execute' if path.endswith('/execute') else 'lookup_or_seal' if '/operations/' in path else 'source_read'
        TRACE.append(dict(surface=label,status=response.status_code))
        return response
    async def aclose(self):await self.inner.aclose()


class RoutedApp:
    app=None
    async def __call__(self,scope,receive,send):return await self.app(scope,receive,send)


class LostPinReply(httpx.AsyncBaseTransport):
    def __init__(self,app):self.inner=httpx.ASGITransport(app=app)
    async def handle_async_request(self,request):
        response=await self.inner.handle_async_request(request)
        await response.aclose()
        raise httpx.ReadTimeout('Controlled lost pin response.')
    async def aclose(self):await self.inner.aclose()


class OwnedDatabaseIO:
    """Fixture owns pool lifecycle; configured root still uses its actual SQL."""
    def __init__(self,database):self.database=database;self.events=[]
    def __getattr__(self,key):return getattr(self.database,key)
    async def open(self):self.events.append('open')
    async def close(self):self.events.append('close')


class Harness(AuthorityHarness):
    async def bridge(self):
        await self.raw_action_read(str(uuid4()))
        await self.policy_kernel()
        self.signing=SigningFixture();self.receipts=Receipts();self.credential=secrets.token_urlsafe(32)
        self.credential_hash=hashlib.sha256(('eai:internal-caller:v1:'+self.credential).encode()).hexdigest()
        actions=['trip.read','claim.read','operation.read','claim.submit','claim.information','claim.decide']
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'workflow-fixture',%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",(uuid4(),self.credential_hash,AUDIENCE,CALLER,Jsonb(actions)))
        await self.execute("INSERT INTO {s}.delegation_keys VALUES(%s,'current',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(ISSUER,Jsonb(self.signing.jwk)))
        for index in (1,4):
            p=self.people[index]
            await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(%s,%s,%s,'approver','process','reimbursement',%s)",(p['tenant'],uuid4(),p['member'],self.people[0]['person']))
            p['token'],p['csrf']=await self.session(p['tenant'],p['person'])
        private=Ed25519PrivateKey.generate();now=int(time.time());key=secrets.token_bytes(32)
        await self.execute("INSERT INTO {s}.receipt_receiver_keys VALUES(%s,'workflow',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(self.credential_hash,key))
        raw_security=json.dumps(dict(credential_hash=self.credential_hash,key_id='workflow',key_hex=key.hex(),not_before=now-60,not_after=now+3600,
            terminal_keys={'workflow':dict(public_hex=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex(),not_before=now-60,not_after=now+3600,revoked=False)}))
        dispatch=secrets.token_urlsafe(32);recovery=secrets.token_urlsafe(32);routed=RoutedApp();self.routed=routed;db_io=OwnedDatabaseIO(self.database)
        issuer='https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test';domain='https://fixture.auth.ap-southeast-1.amazoncognito.com'
        settings=Settings(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url='postgresql://runtime:fixture@db.example.test/platform',auth_enabled=True,
            auth_callback_logging_controlled=True,auth_jwks_background_refresh=False,auth_verifier_key=Fernet.generate_key().decode(),oidc_issuer=issuer,oidc_client_id='fixture',oidc_client_secret='fixture-secret',
            public_origin=ORIGIN,oidc_redirect_uri=ORIGIN+'/api/v1/auth/callback',oidc_authorization_endpoint=domain+'/oauth2/authorize',oidc_token_endpoint=domain+'/oauth2/token',oidc_jwks_uri=issuer+'/.well-known/jwks.json',
            internal_auth_enabled=True,knowledge_enabled=True,browser_context_enabled=True,reimbursement_enabled=True,reimbursement_origin='https://reference-workflow.test',
            reimbursement_dispatcher_credential=dispatch,reimbursement_signing_private_key=self.signing.private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),
            files_enabled=True,files_bucket='owned-receipt-fixture',receipts_enabled=True,receipt_security=raw_security,drafts_enabled=True,actions_enabled=True,flow_enabled=True,reimbursement_recovery_credential=recovery)
        self.flow_settings=settings
        async def close_store():pass
        self.store.close=close_store
        with patch.object(platform_composition,'DecisionSourceReader',lambda target,signer,key,credential_hash:DecisionSourceReader(target,signer,key,credential_hash,transport=httpx.ASGITransport(app=routed))),patch.object(platform_composition,'DraftSourceReader',lambda target,signer,key,credential_hash:DraftSourceReader(target,signer,key,credential_hash,transport=httpx.ASGITransport(app=routed))),patch.object(platform_composition,'RuntimeDatabase',lambda *a,**kw:db_io),patch.object(platform_composition.boto3,'Session',lambda *a,**kw:SimpleNamespace(client=lambda *a,**kw:object())),patch.object(platform_composition,'S3Files',lambda *a,**kw:self.store),patch.object(platform_composition,'ClaimReceiptReader',lambda target,signer:ClaimReceiptReader(target,signer,transport=httpx.ASGITransport(app=routed))):
            resources=platform_composition.build_resources(settings)
        self.delegation=resources.delegation
        with patch.object(platform_main,'build_resources',lambda _:resources):
            public=platform_main.create_app(settings);internal=platform_main.build_app(settings,internal=True)
        self.passed('configured_platform_receipts_flag_builds_actual_shared_DB_API_crypto_download_adapters_with_only_external_IO_fixtures')
        # Fresh independent reference namespace; no installed service upgrade.
        direct=self.reference_target;schema='ref01_probe_'+uuid4().hex
        runtime=replace(direct,user=schema_and_role(schema),password=secrets.token_urlsafe(48))
        reference_db=None;owned=False;client=None
        try:
            async with reference_connection(direct) as connection:
                require(not await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone(),'reference-fresh-namespace')
                require(not await (await connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone(),'reference-fresh-role')
            reference_revision=getattr(self,'reference_revision',5)
            owned=True;require(await reference_migrate(direct,runtime,schema=schema,upgrade=True,target_version=reference_revision)==(reference_revision,reference_revision),'clean-reference-revision')
            reference_db=ReferenceDatabase(runtime,schema,max_size=2)
            async with reference_connection(direct,timeout=300) as admin:
                tenant,owner=str(self.people[0]['tenant']),str(self.people[0]['person']);trip,item,policy_id=str(uuid4()),str(uuid4()),self.policy_fixture['policy']['id']
                day=datetime.now(ZoneInfo('Asia/Singapore')).date()
                body=dict(schema_version=1,digest='0'*64,valid_trip_end_from=(day-timedelta(days=365)).isoformat(),valid_trip_end_to=None,timezone='Asia/Singapore',document_versions=[dict(document_id=str(uuid4()),version_id=str(uuid4()),content_digest='a'*64)],
                    rules=dict(currency='SGD',submission_deadline_days=14,max_total_minor=10000,categories=[dict(code='travel',receipt_required=True,max_amount_minor=5000)],approval_route=[dict(stage=n,approver_id=str(self.people[index]['person']),scope_ref='reimbursement') for n,index in enumerate((1,4),1)]))
                actual=self.policy_fixture['content']
                body.update(valid_trip_end_from=actual['valid_trip_end_from'],valid_trip_end_to=actual['valid_trip_end_to'],timezone=actual['timezone'],document_versions=actual['document_versions'],rules=actual['rules'])
                normalized=json.loads(canonical_body(body));normalized.pop('digest');body['digest']=hashlib.sha256(b'reimbursement:rule-package:v1:'+encoded(dict(tenant_id=tenant,package_id=policy_id,package_version=1,**normalized))).hexdigest()
                package=Package(tenant,policy_id,1,canonical_body(body));policy=dict(id=policy_id,version=1,digest=package.digest)
                async def execute(query,args):return await admin.execute(sql.SQL(query).format(s=sql.Identifier(schema)),args)
                await execute('INSERT INTO {s}.tenant_registry VALUES(%s,%s,true)',(tenant,'a'*64))
                await execute("INSERT INTO {s}.trips VALUES(%s,%s,%s,%s,%s,'Fictional employee','completed')",(tenant,trip,owner,day-timedelta(days=1),day))
                await execute("INSERT INTO {s}.trip_expense_items VALUES(%s,%s,%s,%s,%s,'travel',1000,'SGD')",(tenant,item,trip,owner,day))
                await execute('INSERT INTO {s}.service_policy_packages VALUES(%s,%s,1,%s,1,%s,%s,%s,NULL,%s,%s,%s)',(tenant,policy_id,package.digest,Jsonb(body['rules']),Jsonb(body['document_versions']),body['valid_trip_end_from'],body['timezone'],package.canonical.decode(),now))
                client=PlatformClient(PlatformTarget('https://platform-workflow.test',self.credential),transport=httpx.ASGITransport(app=internal))
                pins=ReceiptPinsClient(client)
                rsettings=ReferenceSettings(database_url='fixture',schema=schema,catalog_enabled=True,claim_access_enabled=True,draft_sources_enabled=True,action_sources_enabled=True,workflow_enabled=True,recovery_credential=recovery,terminal_key_id='workflow',terminal_private_hex=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()).hex(),platform_origin='https://platform-workflow.test',platform_credential=self.credential,dispatcher_credential=dispatch)
                with patch.object(reference_composition,'PlatformClient',lambda target:PlatformClient(target,transport=ObservedTransport(internal))):
                    source=reference_app(rsettings,resource_factory=lambda _:(reference_db,SimpleNamespace()))
                routed.app=source
                async with source.router.lifespan_context(source):
                    async with public.router.lifespan_context(public):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url=ORIGIN) as browser:
                            await self.run_flow(browser,resources,client,pins,source.state.claims,execute,trip,item,policy,body,internal)
                require(db_io.events==['open','close'],'configured-lifecycle-one-owner')
                require(all(c.http.is_closed for c in resources.reimbursement_clients),'configured-http-clients-closed')
                self.passed('configured_platform_and_reference_lifecycles_close_owned_clients_and_only_fixture_pool_owner_remains')
        finally:
            self.store.read_hook=None
            if client is not None:await client.close()
            if reference_db is not None:await reference_db.close()
            if owned:await reference_cleanup(direct,runtime,schema)
        self.passed('owned_reference5_schema_and_role_cleaned_after_actual_cross_system_workflow')



    async def raw_action_read(self,operation):
        async with self.database.transaction() as connection:
            try:return (await (await connection.execute(sql.SQL('SELECT {}.action_call(%s,\'read\',%s)').format(sql.Identifier(self.schema)),(identity_digest('session',self.people[0]['token']),Jsonb(dict(id=operation))))).fetchone())[0]
            except DatabaseError as error:
                import re
                lines=re.findall(r'line (\d+)',getattr(error.diag,'context','') or '')
                raise CheckFailure('action-read-'+str(error.sqlstate)+'-lines-'+','.join(lines)) from None


    async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
        tenant=self.people[0]['tenant'];owner=self.people[0]
        reviewer_id=body['rules']['approval_route'][0]['approver_id']
        member=await self.member(tenant,UUID(reviewer_id),grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')))
        token,csrf=await self.session(tenant,UUID(reviewer_id));self.people.append(dict(tenant=tenant,person=UUID(reviewer_id),member=member,token=token,csrf=csrf));reviewer=len(self.people)-1
        async def headers(index=0):
            p=self.people[index];cookie={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers={**cookie,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});require(me.status_code==200,'flow-bootstrap')
            p['csrf']=me.json()['csrf_token'];return {**cookie,'origin':ORIGIN,'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']}
        uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional actual workflow receipt '*2000,purpose='receipt',mime='application/pdf')
        require(uploaded.status_code==202,'flow-file');await self.process()
        file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
        await self.execute("UPDATE {s}.job_callers SET active=false WHERE tenant_id=%s AND capability='executor'",(tenant,))
        worker_secret=secrets.token_urlsafe(32);job_service=JobService(PostgresJobRepository(self.database),worker_secret)
        await self.execute("INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,'executor',%s,clock_timestamp()+interval '1 hour')",(uuid4(),job_service._credential_hash,tenant))
        def worker():return platform_composition.workflow_worker(self.flow_settings,self.database,worker_secret,transport=ObservedTransport(self.routed))
        async def claim(operation):
            picked=await self.scheduler.claim();require(picked.get('id')==operation and picked['job_type'] in {'action.dispatch','action.reconcile'},'actual-flow-scheduled-job')
            return Lease(UUID(picked['id']),UUID(picked['tenant_id']),picked['generation'])
        async def read(operation,index=0):return await browser.get('/api/v1/actions/'+operation,headers=await headers(index))
        async def draft(kind='submission',cid=None,amount=500):
            response=await browser.post('/api/v1/drafts',json=dict(kind=kind,trip_id=trip,claim_id=cid,conversation_id=None,key=str(uuid4())),headers=await headers())
            require(response.status_code==200,'flow-draft-'+str(response.status_code));id=response.json()['id'];path='/api/v1/drafts/'+id
            content=dict(currency='SGD',items=[dict(id=str(uuid4()),trip_item_id=item,amount_minor=amount,receipt_ids=[file['id']])],receipts=[dict(id=file['id'],version=file['version'])])
            require((await browser.patch(path,json=dict(expected_version=1,key=str(uuid4()),content=content),headers=await headers())).status_code==200,'flow-edit')
            preview=await browser.post(path+'/preview',json=dict(expected_version=2),headers=await headers());require(preview.status_code==200,'flow-preview')
            confirmed=await browser.post(path+'/confirm',json=dict(expected_version=2,material_hash=preview.json()['material_hash'],key=str(uuid4())),headers=await headers())
            require(confirmed.status_code==202,'flow-confirm-'+str(confirmed.status_code));return id,confirmed.json()['operation_id']
        draft_id,op=await draft();lease=await claim(op);w=worker()
        try:result=await w.process(lease)
        finally:await w.transport.close()
        require(result.get('state')=='committed','flow-submit-'+str(result.get('state')))
        current=(await read(op)).json();cid=current['result_ref'];require(current['state']=='committed' and current['result_version']==1,'flow-current-result')
        require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(draft_id,))=='committed','flow-draft-commit')
        require(await self.scalar('SELECT state FROM {s}.receipt_pin_batches WHERE operation_id=%s',(op,))=='finalised','flow-pin-atomic-finalise')
        self.passed('actual_scheduler_worker_grant_source_HTTP_live_policy_receipt_pin_and_ledger_commit_settle_action_draft_and_pin_in_one_platform_transaction')
        decision=dict(expected_version=1,revision=1,stage=1,decision='request_information',reason='Fictional workflow clarification',key=str(uuid4()))
        response=await browser.post('/api/v1/claims/'+cid+'/decisions',json=decision,headers=await headers(reviewer))
        require(response.status_code==202,'flow-decision-confirm');dop=response.json()['operation_id']
        owned=await self.scalar('SELECT to_jsonb(a) FROM {s}.actions a WHERE id=%s',(dop,))
        binding=await self.scalar('SELECT to_jsonb(b) FROM {s}.action_claim_bindings b WHERE operation_id=%s',(dop,))
        require(binding is not None,'flow-durable-decision-binding-present')
        actor=dict(actor_id=owned['actor_id'],tenant_id=owned['tenant_id'],membership_id=owned['membership_id'],access_revision=owned['access_revision'],operation_id=dop,request_hash=owned['canonical_hash'])
        async with self.database.transaction() as connection:
            try:
                check=(await (await connection.execute(sql.SQL('SELECT {}.flow_locked_call(%s,%s,%s,%s,%s)').format(sql.Identifier(self.schema)),(Jsonb(actor),Jsonb(owned['payload']),Jsonb(binding['policy']),binding['trip_date'],owned['claim_ref']))).fetchone())[0]
            except DatabaseError as error:raise CheckFailure('flow-locked-rpc-'+str(error.sqlstate)) from None
        require(check is True,'flow-local-locked-decision-binding-current')
        lease=await claim(dop);w=worker()
        try:result=await w.process(lease)
        finally:await w.transport.close()
        require(result.get('state')=='committed' and result['result']['code']=='information_requested','flow-real-locked-decision-'+str(result.get('state')))
        _,iop=await draft('information_update',cid,550);lease=await claim(iop);w=worker()
        try:result=await w.process(lease)
        finally:await w.transport.close()
        require(result.get('state')=='committed' and result['result']['version']==3,'flow-info-actual-live-policy-'+str(result.get('state')))
        self.passed('actual_decision_and_information_workers_use_immutable_ACT_claim_binding_and_live_nonrecursive_policy_eligibility_to_commit_correct_revision')
        # A second submission on reserved items is an immutable ledger reject.
        _,bad=await draft();lease=await claim(bad);w=worker()
        try:result=await w.process(lease)
        finally:await w.transport.close()
        require(result.get('state')=='unknown','flow-known-rejection-held')
        require(await self.scalar('SELECT state FROM {s}.outbox_jobs WHERE id=%s',(bad,))=='held','flow-rejection-no-loop')
        require(await self.scalar('SELECT state FROM {s}.receipt_pin_batches WHERE operation_id=%s',(bad,))=='held','flow-rejected-no-release')
        self.passed('generic_signed_rejection_is_held_without_unfreezing_releasing_pins_or_futile_automatic_seal_loop')
        # New owned trip item avoids the intentional business duplicate above.
        newer=str(uuid4());await execute("INSERT INTO {s}.trip_expense_items VALUES(%s,%s,%s,%s,%s,'travel',1000,'SGD')",(str(tenant),newer,trip,str(owner['person']),datetime.now(ZoneInfo('Asia/Singapore')).date()))
        item=newer;lost_draft,lost_op=await draft();lease=await claim(lost_op)
        class LostReply(httpx.AsyncBaseTransport):
            def __init__(inner):inner.transport=httpx.ASGITransport(app=self.routed)
            async def handle_async_request(inner,request):
                response=await inner.transport.handle_async_request(request);await response.aclose();raise httpx.ReadTimeout('Controlled response loss')
            async def aclose(inner):await inner.transport.aclose()
        w=platform_composition.workflow_worker(self.flow_settings,self.database,worker_secret,transport=LostReply())
        try:lost=await w.process(lease)
        finally:await w.transport.close()
        require(lost.get('state')=='unknown' and (await read(lost_op)).json()['state']=='unknown','flow-lost-response-unknown')
        await self.execute('UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp() WHERE id=%s',(lost_op,))
        fresh=await claim(lost_op);w=worker()
        try:
            recovered=await w.process(fresh)
            try:await w.process(lease)
            except FlowFailure as error:require(error.code=='stale','flow-stale-generation-denied')
            else:raise CheckFailure('stale-flow-worker-accepted')
        finally:await w.transport.close()
        require(recovered.get('state')=='committed','flow-lost-recover-commit-'+str(recovered.get('state')))
        require(await self.scalar('SELECT count(*) FROM {s}.flow_results WHERE operation_id=%s',(lost_op,))==1,'one-local-terminal-after-restart')
        rows=await (await execute('SELECT count(*) FROM {s}.claims WHERE trip_id=%s',(trip,))).fetchone();require(rows==(2,),'no-duplicate-source-claim')
        self.passed('lost_source_commit_response_persists_unknown_and_new_worker_generation_uses_lookup_to_recover_exact_once_with_old_lease_fenced')
        # Crash after preparing an intent but before sending: lookup404 must seal.
        absent_draft,absent=await draft();lease=await claim(absent);w=worker()
        try:
            await w.call('prepare',lease)
            resolved=await w.process(lease)
        finally:await w.transport.close()
        require(resolved.get('state')=='not_committed','flow-404-then-seal-'+str(resolved.get('state')))
        require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(absent_draft,))=='not_committed','flow-atomic-seal-draft')
        self.passed('restart_before_send_performs_lookup404_then_distinct_recovery_credential_atomic_seal_before_marking_local_not_committed')


def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned database fixtures.')
    folder=ROOT/'local/flow01';folder.mkdir(parents=True,exist_ok=True);report={'passed':False}
    try:
        files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))
        files+=list((ROOT/'scripts/checks').glob('check_knowledge_workspace.py'))+[Path(__file__),ROOT/'scripts/checks/check_policy_eligibility.py',ROOT/'scripts/checks/check_policy_draft.py',ROOT/'scripts/checks/check_claims.py']
        hashes={str(p.resolve().relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=26,operation='bridge',direct_timeout=400))
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'workflow-source-stable')
        report.update(source_sha256=hashes,scope='actual configured platform26/reference5 leased dispatch/recovery/settlement, original grants, live policy/pins/ledger; storage/parser/HTTPS ASGI transport fixtures; no cloud/model acceptance')
    except Exception as error:
        report.update(error_type=type(error).__name__)
        report['transport_statuses']=TRACE[-20:]
        if isinstance(error,CheckFailure):report['check']=str(error)
        frames=[];current=error
        for _ in range(12):
            if current is None:break
            frames.extend(dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__))
            if type(getattr(current,'status',None)) is int:report['dependency_status']=current.status
            code=getattr(current,'sqlstate',None)
            if code:
                report['sqlstate']=code;position=getattr(getattr(current,'diag',None),'statement_position',None)
                if str(position).isdigit():report['statement_position']=int(position)
            current=current.__context__
        report['frames']=frames
    (folder/'workflow-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
