"""Owned local FILE02 workflow: configured APIs, real pins/claim ledger/download.

ACT receipt issuance, policy eligibility, blob storage/parser and HTTP transports
are controlled fixtures. Both applications, RSA/Ed25519/MAC and SQL are actual.
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
        self.signing=SigningFixture();self.receipts=Receipts();self.credential=secrets.token_urlsafe(32)
        self.credential_hash=hashlib.sha256(('eai:internal-caller:v1:'+self.credential).encode()).hexdigest()
        actions=['trip.read','claim.read','operation.read','claim.submit','claim.information','claim.decide']
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'workflow-fixture',%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",(uuid4(),self.credential_hash,AUDIENCE,CALLER,Jsonb(actions)))
        await self.execute("INSERT INTO {s}.delegation_keys VALUES(%s,'current',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(ISSUER,Jsonb(self.signing.jwk)))
        await self.execute('CREATE TABLE {s}.receipt_human_fixture(id uuid PRIMARY KEY,claims jsonb NOT NULL,revoked boolean NOT NULL DEFAULT false)')
        await self.execute('REVOKE ALL ON {s}.receipt_human_fixture FROM PUBLIC')
        await self.execute('''CREATE OR REPLACE FUNCTION {s}._receipt_human_current(claims jsonb,payload jsonb) RETURNS boolean
          LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $f$
          #variable_conflict use_variable
          DECLARE r record;BEGIN SELECT * INTO r FROM {s}.receipt_human_fixture WHERE id=(claims->>'human_receipt_id')::uuid FOR SHARE;
          RETURN r.id IS NOT NULL AND NOT r.revoked AND r.claims=claims;END $f$''')
        for index in (1,4):
            p=self.people[index]
            await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(%s,%s,%s,'approver','process','reimbursement',%s)",(p['tenant'],uuid4(),p['member'],self.people[0]['person']))
            p['token'],p['csrf']=await self.session(p['tenant'],p['person'])
        private=Ed25519PrivateKey.generate();now=int(time.time());key=secrets.token_bytes(32)
        await self.execute("INSERT INTO {s}.receipt_receiver_keys VALUES(%s,'workflow',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(self.credential_hash,key))
        raw_security=json.dumps(dict(credential_hash=self.credential_hash,key_id='workflow',key_hex=key.hex(),not_before=now-60,not_after=now+3600,
            terminal_keys={'workflow':dict(public_hex=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex(),not_before=now-60,not_after=now+3600,revoked=False)}))
        dispatch=secrets.token_urlsafe(32);routed=RoutedApp();db_io=OwnedDatabaseIO(self.database)
        issuer='https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test';domain='https://fixture.auth.ap-southeast-1.amazoncognito.com'
        settings=Settings(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url='postgresql://runtime:fixture@db.example.test/platform',auth_enabled=True,
            auth_callback_logging_controlled=True,auth_jwks_background_refresh=False,auth_verifier_key=Fernet.generate_key().decode(),oidc_issuer=issuer,oidc_client_id='fixture',oidc_client_secret='fixture-secret',
            public_origin=ORIGIN,oidc_redirect_uri=ORIGIN+'/api/v1/auth/callback',oidc_authorization_endpoint=domain+'/oauth2/authorize',oidc_token_endpoint=domain+'/oauth2/token',oidc_jwks_uri=issuer+'/.well-known/jwks.json',
            internal_auth_enabled=True,knowledge_enabled=True,browser_context_enabled=True,reimbursement_enabled=True,reimbursement_origin='https://reference-workflow.test',
            reimbursement_dispatcher_credential=dispatch,reimbursement_signing_private_key=self.signing.private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),
            files_enabled=True,files_bucket='owned-receipt-fixture',receipts_enabled=True,receipt_security=raw_security)
        async def close_store():pass
        self.store.close=close_store
        with patch.object(platform_composition,'RuntimeDatabase',lambda *a,**kw:db_io),patch.object(platform_composition.boto3,'Session',lambda *a,**kw:SimpleNamespace(client=lambda *a,**kw:object())),patch.object(platform_composition,'S3Files',lambda *a,**kw:self.store),patch.object(platform_composition,'ClaimReceiptReader',lambda target,signer:ClaimReceiptReader(target,signer,transport=httpx.ASGITransport(app=routed))):
            resources=platform_composition.build_resources(settings)
        resources.delegation.receipts=self.receipts
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
            owned=True;require(await reference_migrate(direct,runtime,schema=schema,upgrade=True)==(5,5),'clean-reference5')
            reference_db=ReferenceDatabase(runtime,schema,max_size=2)
            async with reference_connection(direct,timeout=300) as admin:
                tenant,owner=str(self.people[0]['tenant']),str(self.people[0]['person']);trip,item,policy_id=str(uuid4()),str(uuid4()),str(uuid4())
                day=datetime.now(ZoneInfo('Asia/Singapore')).date()
                body=dict(schema_version=1,digest='0'*64,valid_trip_end_from=(day-timedelta(days=365)).isoformat(),valid_trip_end_to=None,timezone='Asia/Singapore',document_versions=[dict(document_id=str(uuid4()),version_id=str(uuid4()),content_digest='a'*64)],
                    rules=dict(currency='SGD',submission_deadline_days=14,max_total_minor=10000,categories=[dict(code='travel',receipt_required=True,max_amount_minor=5000)],approval_route=[dict(stage=n,approver_id=str(self.people[index]['person']),scope_ref='reimbursement') for n,index in enumerate((1,4),1)]))
                normalized=json.loads(canonical_body(body));normalized.pop('digest');body['digest']=hashlib.sha256(b'reimbursement:rule-package:v1:'+encoded(dict(tenant_id=tenant,package_id=policy_id,package_version=1,**normalized))).hexdigest()
                package=Package(tenant,policy_id,1,canonical_body(body));policy=dict(id=policy_id,version=1,digest=package.digest)
                async def execute(query,args):return await admin.execute(sql.SQL(query).format(s=sql.Identifier(schema)),args)
                await execute('INSERT INTO {s}.tenant_registry VALUES(%s,%s,true)',(tenant,'a'*64))
                await execute("INSERT INTO {s}.trips VALUES(%s,%s,%s,%s,%s,'Fictional employee','completed')",(tenant,trip,owner,day-timedelta(days=1),day))
                await execute("INSERT INTO {s}.trip_expense_items VALUES(%s,%s,%s,%s,%s,'travel',1000,'SGD')",(tenant,item,trip,owner,day))
                await execute('INSERT INTO {s}.service_policy_packages VALUES(%s,%s,1,%s,1,%s,%s,%s,NULL,%s,%s,%s)',(tenant,policy_id,package.digest,Jsonb(body['rules']),Jsonb(body['document_versions']),body['valid_trip_end_from'],body['timezone'],package.canonical.decode(),now))
                client=PlatformClient(PlatformTarget('https://platform-workflow.test',self.credential),transport=httpx.ASGITransport(app=internal))
                pins=ReceiptPinsClient(client)
                class Authority:
                    async def authorize(inner,payload,actor,*,operation_key,candidate_claim_id,policy,trip_date,claim,context):
                        stamp=int(time.time());receipt_pins=[];expiry=min(actor.expires_at,stamp+35)
                        if payload['action'] in {'claim.submit','claim.information'}:
                            pinned=await pins.pin(context['delegation'],payload,operation_key=operation_key,candidate_claim_id=candidate_claim_id,actor_expires_at=actor.expires_at)
                            expiry=min(expiry,pinned['expires_at']);receipt_pins=pinned['pins']
                        kind={'claim.submit':'submission','claim.information':'information','claim.decide':'decision'}[payload['action']]
                        eligibility=dict(allow=True,policy=policy,operation_id=actor.operation_id,request_hash=actor.request_hash,operation_kind=kind,trip_date=trip_date,claim_ref=None if kind=='submission' else candidate_claim_id,eligibility_revision=1,checked_at=stamp,expires_at=expiry,blocked_reason=None)
                        return ClaimPermit(encoded(dict(request_hash=actor.request_hash,operation_id=actor.operation_id,operation_key=operation_key,candidate_claim_id=candidate_claim_id,expires_at=expiry,human_receipt_id=context['human_receipt_id'],human_action_ref=context['human_receipt_id'],eligibility=eligibility,receipt_pins=receipt_pins)))
                signer=Signer(kid='workflow',private_bytes=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()))
                service=claims_service(reference_db,signer,authority=Authority())
                rsettings=ReferenceSettings(database_url='fixture',schema=schema,catalog_enabled=True,claim_access_enabled=True,platform_origin='https://platform-workflow.test',platform_credential=self.credential,dispatcher_credential=dispatch)
                with patch.object(reference_composition,'PlatformClient',lambda target:PlatformClient(target,transport=httpx.ASGITransport(app=internal))):
                    source=reference_app(rsettings,resource_factory=lambda _:(reference_db,SimpleNamespace()))
                routed.app=source
                async with source.router.lifespan_context(source):
                    async with public.router.lifespan_context(public):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url=ORIGIN) as browser:
                            await self.run_flow(browser,resources,client,pins,service,execute,trip,item,policy,body,internal)
                require(db_io.events==['open','close'],'configured-lifecycle-one-owner')
                require(all(c.http.is_closed for c in resources.reimbursement_clients),'configured-http-clients-closed')
                self.passed('configured_platform_and_reference_lifecycles_close_owned_clients_and_only_fixture_pool_owner_remains')
        finally:
            self.store.read_hook=None
            if client is not None:await client.close()
            if reference_db is not None:await reference_db.close()
            if owned:await reference_cleanup(direct,runtime,schema)
        self.passed('owned_reference5_schema_and_role_cleaned_after_actual_cross_system_workflow')

    async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
        uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional workflow receipt '*2000,purpose='receipt',mime='application/pdf')
        require(uploaded.status_code==202,'bridge-upload');await self.process()
        file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
        data=dict(trip_id=trip,currency='SGD',items=[dict(id=str(uuid4()),trip_item_id=item,amount_minor=500,receipt_ids=[file['id']])],total_minor=500,
            receipts=[dict(id=file['id'],version=file['version'],digest=file['digest'])],policy=policy,approval_route=body['rules']['approval_route'],destination=AUDIENCE)
        payload=dict(schema_version=1,action='claim.submit',tenant_id=str(self.people[0]['tenant']),actor_id=str(self.people[0]['person']),operation_id=str(uuid4()),object=dict(kind='draft',id=str(uuid4()),version=1),data=data)
        async def authorise(payload,index):
            token,claims=await self.authority(self.people[index],payload)
            await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(claims['human_receipt_id'],Jsonb(claims)))
            return await client.verify(token,payload),dict(delegation=token,human_receipt_id=claims['human_receipt_id'])
        actor,context=await authorise(payload,0);key=str(uuid4())
        original=client.http
        client.http=httpx.AsyncClient(base_url=original.base_url,transport=LostPinReply(internal),trust_env=False,follow_redirects=False,
            limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))
        try:
            try:await service.execute(payload,key,actor,context=context)
            except ClaimFailure as error:require(error.status==503,'lost-pin-response-is-unknown')
            else:raise CheckFailure('controlled-lost-pin-response-not-observed')
        finally:
            await client.http.aclose();client.http=original
        held=await resources.receipts.repository.lookup(self.credential_hash,payload['tenant_id'],payload['operation_id'])
        no_commit=(await (await execute('SELECT count(*) FROM {s}.service_operations WHERE operation_id=%s',(payload['operation_id'],))).fetchone())[0]
        require(held['state']=='held' and no_commit==0,'lost-pin-before-ledger-keeps-held')
        committed=await service.execute(payload,key,actor,context=context);cid=committed['operation']['result']['ref']
        row=await resources.receipts.repository.lookup(self.credential_hash,payload['tenant_id'],payload['operation_id'])
        require(row['state']=='held' and row['batch']['claim_id']==held['batch']['claim_id']==cid and row['batch']['identity']['key']==key,'pin-and-ledger-same-candidate')
        self.passed('lost_pin_response_before_any_claim_ledger_record_keeps_held_pin_and_retry_reuses_exact_stable_candidate_before_commit')
        settled=await pins.finalise(payload['tenant_id'],payload['operation_id'],committed['proof'])
        require(settled['state']=='finalised','real-ledger-finalise')
        before=await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')
        again=await service.execute(payload,key,actor,context=context)
        require(again['operation']==committed['operation'] and await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')==before,'real-ledger-replay-no-new-pin')
        self.passed('actual_platform_pin_API_candidate_and_operation_key_flow_into_service5_claim_ledger_proof_and_platform_finalise_with_exact_replay')
        async def headers(index):
            p=self.people[index];cookie={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers={**cookie,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'})
            require(me.status_code==200,'browser-context-bootstrap-'+str(index))
            return {**cookie,'x-eai-expected-context':me.json()['context_binding']}
        path='/api/v1/files/'+file['id']+'/claims/'+cid+'/content'
        expected=self.store.objects[file['object_key']]['data']
        for index in (0,1):
            response=await browser.get(path,headers=await headers(index))
            require(response.status_code==200,'actual-claim-receipt-download-'+str(index)+'-'+str(response.status_code))
            require(response.content==expected and response.headers.get('cache-control')=='no-store' and response.headers.get('content-disposition','').startswith('attachment;'),'receipt-bounded-attachment')
        self.passed('configured_owner_and_current_reviewer_download_exact_ready_pinned_receipt_through_current_source_API_and_MAC_SQL_browser_binding')
        for index in (2,3,4):
            response=await browser.get(path,headers=await headers(index))
            require(response.status_code in {403,404},'foreign-admin-future-reviewer-denied-'+str(index))
            require(expected not in response.content,'denied-no-receipt-bytes')
        require((await browser.get(path)).status_code in {400,401},'missing-browser-binding-denied')
        self.passed('foreign_tenant_admin_and_future_stage_general_approver_never_gain_receipt_bytes_or_bypass_original_browser_binding')
        token_hash=identity_digest('session',self.people[0]['token'])
        context=(await resources.files.repository.claim_call(token_hash,'context',{}))['context']
        _,valid=await resources.files.claim_authority.authorize(context,cid)
        forged=copy.deepcopy(valid);forged['proof']='0'*64
        try:await resources.files.repository.claim_call(token_hash,'reference',dict(id=file['id'],claim_id=cid,attestation=forged))
        except FileFailure as error:require(error.status==403,'forged-download-MAC-denied')
        else:raise CheckFailure('forged-download-MAC-must-not-read-metadata')
        await self.execute('UPDATE {s}.receipt_receiver_keys SET active=false')
        denied=await browser.get(path,headers=await headers(0))
        require(denied.status_code==403 and expected not in denied.content,'revoked-download-key-no-bytes')
        await self.execute('UPDATE {s}.receipt_receiver_keys SET active=true')
        self.passed('SQL_download_rejects_fabricated_relation_MAC_and_current_key_revocation_with_no_private_receipt_bytes')
        async def revoke_session():
            self.store.read_hook=None
            await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s',(token_hash,))
        self.store.read_hook=revoke_session
        denied=await browser.get(path,headers=await headers(0))
        require(denied.status_code in {401,403} and expected not in denied.content,'session-revoked-after-storage-no-bytes')
        self.people[0]['token'],self.people[0]['csrf']=await self.session(self.people[0]['tenant'],self.people[0]['person'])
        self.passed('actual_browser_session_revocation_during_storage_read_blocks_source_reauthorization_and_buffer_release')
        decision=dict(schema_version=1,action='claim.decide',tenant_id=payload['tenant_id'],actor_id=str(self.people[1]['person']),operation_id=str(uuid4()),object=dict(kind='claim',id=cid,version=1),data=dict(revision=1,stage=1,decision='approve',reason=''))
        async def advance():
            self.store.read_hook=None
            current,ctx=await authorise(decision,1)
            result=await service.execute(decision,str(uuid4()),current,context=ctx)
            require(result['operation']['result']['code']=='stage_approved','real-stage-advanced')
        self.store.read_hook=advance
        denied=await browser.get(path,headers=await headers(1))
        require(denied.status_code in {403,404} and expected not in denied.content,'reviewer-revoke-after-storage-no-bytes')
        allowed=await browser.get(path,headers=await headers(4))
        require(allowed.status_code==200 and allowed.content==expected,'new-active-reviewer-gets-receipt')
        self.passed('actual_claim_stage_advance_during_storage_read_revokes_old_reviewer_before_buffer_release_and_new_active_reviewer_succeeds')
        need=dict(schema_version=1,action='claim.decide',tenant_id=payload['tenant_id'],actor_id=str(self.people[4]['person']),operation_id=str(uuid4()),object=dict(kind='claim',id=cid,version=2),data=dict(revision=1,stage=2,decision='request_information',reason='Fictional replacement receipt requested'))
        need_actor,need_context=await authorise(need,4)
        requested=await service.execute(need,str(uuid4()),need_actor,context=need_context)
        require(requested['operation']['result']['version']==3,'actual-information-request-version')
        replacement=await self.upload(b'%PDF-1.4\nFictional replacement receipt',purpose='receipt',mime='application/pdf')
        require(replacement.status_code==202,'replacement-receipt-upload');await self.process()
        newer=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(replacement.json()['id'],))
        revised=copy.deepcopy(data);revised['items'][0]['receipt_ids']=[newer['id']]
        revised.update(receipts=[dict(id=newer['id'],version=newer['version'],digest=newer['digest'])],revision=1,draft=dict(id=str(uuid4()),version=1))
        information=dict(schema_version=1,action='claim.information',tenant_id=payload['tenant_id'],actor_id=payload['actor_id'],operation_id=str(uuid4()),object=dict(kind='claim',id=cid,version=3),data=revised)
        info_actor,info_context=await authorise(information,0)
        received=await service.execute(information,str(uuid4()),info_actor,context=info_context)
        require(received['operation']['result']['ref']==cid and received['operation']['result']['version']==4,'actual-info-same-claim-next-version')
        require((await pins.finalise(information['tenant_id'],information['operation_id'],received['proof']))['state']=='finalised','actual-info-pin-finalise')
        old=await browser.get(path,headers=await headers(0))
        require(old.status_code==404 and expected not in old.content,'old-revision-file-not-current-claim-source')
        new_path='/api/v1/files/'+newer['id']+'/claims/'+cid+'/content'
        for index in (0,1):
            current=await browser.get(new_path,headers=await headers(index))
            require(current.status_code==200 and current.content==self.store.objects[newer['object_key']]['data'],'current-information-receipt-authorized')
        require((await browser.get(new_path,headers=await headers(4))).status_code in {403,404},'route-reset-future-reviewer-denied')
        self.passed('actual_information_update_pins_replacement_receipt_commits_same_claim_next_revision_finalises_and_revokes_old_revision_file_access_with_route_reset')


def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned database fixtures.')
    folder=ROOT/'local/file02';report={'passed':False}
    try:
        files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))
        files+=list((ROOT/'scripts/checks').glob('check_knowledge_workspace.py'))+[Path(__file__),ROOT/'scripts/checks/check_policy_eligibility.py',ROOT/'scripts/checks/check_policy_draft.py',ROOT/'scripts/checks/check_claims.py']
        hashes={str(p.resolve().relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=23,operation='bridge',direct_timeout=400))
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'workflow-source-stable')
        report.update(source_sha256=hashes,scope='actual configured platform23/reference5 pin→claim ledger→finalise and current download; ACT issuance/policy eligibility/storage/parser/HTTP transport controlled fixtures; no cloud/model acceptance')
    except Exception as error:
        report.update(error_type=type(error).__name__)
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
