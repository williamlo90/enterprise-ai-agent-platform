"""Owned ACT01 workflow: configured APIs and durable human authority.

Policy eligibility, storage/parser and HTTP transports are controlled fixtures.
ACT issuance/storage/gateway and both applications/crypto/SQL are actual.
Dispatch is invoked explicitly by this check; FLOW workers are a later task.
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
        dispatch=secrets.token_urlsafe(32);routed=RoutedApp();db_io=OwnedDatabaseIO(self.database)
        issuer='https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test';domain='https://fixture.auth.ap-southeast-1.amazoncognito.com'
        settings=Settings(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url='postgresql://runtime:fixture@db.example.test/platform',auth_enabled=True,
            auth_callback_logging_controlled=True,auth_jwks_background_refresh=False,auth_verifier_key=Fernet.generate_key().decode(),oidc_issuer=issuer,oidc_client_id='fixture',oidc_client_secret='fixture-secret',
            public_origin=ORIGIN,oidc_redirect_uri=ORIGIN+'/api/v1/auth/callback',oidc_authorization_endpoint=domain+'/oauth2/authorize',oidc_token_endpoint=domain+'/oauth2/token',oidc_jwks_uri=issuer+'/.well-known/jwks.json',
            internal_auth_enabled=True,knowledge_enabled=True,browser_context_enabled=True,reimbursement_enabled=True,reimbursement_origin='https://reference-workflow.test',
            reimbursement_dispatcher_credential=dispatch,reimbursement_signing_private_key=self.signing.private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),
            files_enabled=True,files_bucket='owned-receipt-fixture',receipts_enabled=True,receipt_security=raw_security,drafts_enabled=True,actions_enabled=True)
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
            owned=True;require(await reference_migrate(direct,runtime,schema=schema,upgrade=True)==(5,5),'clean-reference5')
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
                rsettings=ReferenceSettings(database_url='fixture',schema=schema,catalog_enabled=True,claim_access_enabled=True,draft_sources_enabled=True,action_sources_enabled=True,platform_origin='https://platform-workflow.test',platform_credential=self.credential,dispatcher_credential=dispatch)
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



    async def raw_action_read(self,operation):
        async with self.database.transaction() as connection:
            try:return (await (await connection.execute(sql.SQL('SELECT {}.action_call(%s,\'read\',%s)').format(sql.Identifier(self.schema)),(identity_digest('session',self.people[0]['token']),Jsonb(dict(id=operation))))).fetchone())[0]
            except DatabaseError as error:
                import re
                lines=re.findall(r'line (\d+)',getattr(error.diag,'context','') or '')
                raise CheckFailure('action-read-'+str(error.sqlstate)+'-lines-'+','.join(lines)) from None

    async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
        require(isinstance(resources.delegation.receipts,PostgresHumanReceiptReader),'actual-human-receipt-reader-configured')
        route=body['rules']['approval_route'][0]['approver_id'];tenant=self.people[0]['tenant']
        member=await self.member(tenant,UUID(route),grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')))
        token,csrf=await self.session(tenant,UUID(route));self.people.append(dict(tenant=tenant,person=UUID(route),member=member,token=token,csrf=csrf));reviewer=len(self.people)-1
        async def headers(index=0):
            p=self.people[index];cookie={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers={**cookie,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'})
            require(me.status_code==200,'action-bootstrap-'+str(index));p['csrf']=me.json()['csrf_token']
            return {**cookie,'x-eai-expected-context':me.json()['context_binding'],'origin':ORIGIN,'content-type':'application/json','x-csrf-token':p['csrf']}
        uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional human-confirmed receipt '*2000,purpose='receipt',mime='application/pdf')
        require(uploaded.status_code==202,'action-receipt-upload');await self.process()
        file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
        content=dict(currency='SGD',items=[dict(id=str(uuid4()),trip_item_id=item,amount_minor=500,receipt_ids=[file['id']])],receipts=[dict(id=file['id'],version=file['version'])])
        async def prepare():
            created=await browser.post('/api/v1/drafts',json=dict(kind='submission',trip_id=trip,claim_id=None,conversation_id=None,key=str(uuid4())),headers=await headers())
            require(created.status_code==200,'action-draft-create-'+str(created.status_code));draft=created.json();path='/api/v1/drafts/'+draft['id']
            edited=await browser.patch(path,json=dict(expected_version=1,key=str(uuid4()),content=content),headers=await headers())
            require(edited.status_code==200,'action-draft-edit')
            preview=await browser.post(path+'/preview',json=dict(expected_version=2),headers=await headers())
            require(preview.status_code==200,'action-preview-'+str(preview.status_code))
            return draft,path,dict(expected_version=2,material_hash=preview.json()['material_hash'],key=str(uuid4()))
        draft,path,command=await prepare()
        require(await self.scalar('SELECT count(*) FROM {s}.human_action_receipts')==0,'preview-no-receipt')
        mismatch=await browser.post(path+'/confirm',json={**command,'material_hash':'f'*64},headers=await headers())
        require(mismatch.status_code==409,'confirmation-exact-material-hash-'+str(mismatch.status_code))
        require(await self.scalar('SELECT count(*) FROM {s}.actions')==0,'mismatch-no-actions')
        self.passed('preview_never_issues_human_receipt_and_mismatched_material_confirmation_has_no_action_or_consent')
        await self.execute("CREATE FUNCTION {s}._action_fixture_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Fixture audit insert failure' USING ERRCODE='23514';END $$")
        await self.execute('REVOKE ALL ON FUNCTION {s}._action_fixture_fail() FROM PUBLIC,{a}')
        await self.execute('CREATE TRIGGER action_fixture_fail BEFORE INSERT ON {s}.action_audit FOR EACH ROW EXECUTE FUNCTION {s}._action_fixture_fail()')
        try:
            failed=await browser.post(path+'/confirm',json=command,headers=await headers())
            require(failed.status_code==400,'injected-audit-failure-'+str(failed.status_code))
            for table in ('actions','human_action_receipts','consents','action_audit'):
                require(await self.scalar('SELECT count(*) FROM {s}.'+table)==0,'atomic-rollback-'+table)
            require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE job_type='action.dispatch'")==0,'atomic-rollback-outbox')
            require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(draft['id'],))=='editable','atomic-rollback-freeze')
        finally:
            await self.execute('DROP TRIGGER action_fixture_fail ON {s}.action_audit');await self.execute('DROP FUNCTION {s}._action_fixture_fail()')
        confirmed=await browser.post(path+'/confirm',json=command,headers=await headers())
        require(confirmed.status_code==202,'human-confirm-'+str(confirmed.status_code));action=confirmed.json();operation=action['operation_id']
        for table in ('actions','human_action_receipts','consents','action_audit'):
            require(await self.scalar('SELECT count(*) FROM {s}.'+table)==1,'atomic-one-'+table)
        require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE job_type='action.dispatch' AND target_id=%s",(operation,))==1,'one-durable-dispatch-job')
        require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(draft['id'],))=='frozen','confirmed-freeze')
        self.passed('actual_HTTP_confirmation_atomically_freezes_and_writes_consent_receipt_action_JOB_outbox_and_audit_with_injected_mid_transaction_rollback')
        original=resources.actions.drafts.read
        async def outage(*args):raise RuntimeError('Controlled source outage')
        resources.actions.drafts.read=outage
        try:
            replay=await browser.post(path+'/confirm',json=command,headers=await headers())
            require(replay.status_code==202 and replay.json()==action,'durable-replay-without-source')
        finally:resources.actions.drafts.read=original
        altered=await browser.post(path+'/confirm',json={**command,'material_hash':'f'*64},headers=await headers())
        require(altered.status_code==409,'reused-key-changed-material')
        require((await browser.patch(path,json=dict(expected_version=2,key=str(uuid4()),content=content),headers=await headers())).status_code==409,'frozen-no-edit')
        require((await browser.post(path+'/confirm',json={**command,'key':str(uuid4())},headers=await headers())).status_code==409,'frozen-no-second-action')
        require((await self.raw_action_read(operation)).get('status')=='ok','raw-durable-action-read')
        state=await browser.get('/api/v1/actions/'+operation,headers=await headers());require(state.status_code==200 and state.json()==action,'actual-action-status-'+str(state.status_code)+'-same-'+str(state.json()==action))
        for index in (1,2,3):require((await browser.get('/api/v1/actions/'+operation,headers=await headers(index))).status_code==404,'action-not-other-actor')
        self.passed('durable_key_replay_survives_source_outage_frozen_draft_cannot_change_or_issue_second_action_and_current_status_is_actor_scoped')
        async def authorise(operation):
            row=await self.scalar('SELECT to_jsonb(a) FROM {s}.actions a WHERE id=%s',(operation,));payload=row['payload']
            require(request_hash(payload)==row['canonical_hash'],'independent-human-payload-canonical-hash')
            receipt=await resources.delegation.receipts.get(row['human_receipt_id']);require(receipt is not None,'actual-persisted-receipt-reader')
            # Delegation cannot predate the durable human event. A small DB /
            # workstation clock offset is within AUTH04's existing skew bound.
            now=max(int(time.time()),receipt.human_action_time);claims=dict(iss=ISSUER,aud=AUDIENCE,caller=CALLER,sub=row['actor_id'],tenant_id=row['tenant_id'],membership_id=row['membership_id'],session_id=row['session_id'],access_revision=row['access_revision'],action=payload['action'],object_kind=payload['object']['kind'],object_id=payload['object']['id'],object_version=payload['object']['version'],operation_id=operation,request_hash=row['canonical_hash'],jti=str(uuid4()),iat=now,nbf=now,exp=now+60,human_receipt_id=row['human_receipt_id'])
            mismatches=[a for a,b in (('id','human_receipt_id'),('actor_id','sub'),('session_id','session_id'),('tenant_id','tenant_id'),('operation_id','operation_id'),('action','action'),('object_kind','object_kind'),('object_id','object_id'),('object_version','object_version'),('request_hash','request_hash')) if getattr(receipt,a)!=claims[b]]
            require(receipt.matches(claims),'actual-receipt-exact-claims-fields-'+','.join(mismatches))
            token=self.signing.sign(claims)
            return payload,await client.verify(token,payload),dict(delegation=token,human_receipt_id=row['human_receipt_id']),row,claims
        payload,actor,context,row,claims=await authorise(operation)
        committed=await service.execute(payload,row['key'],actor,context=context)
        require(committed['operation']['state']=='committed','real-receipt-source-submit')
        cid=committed['operation']['result']['ref']
        require((await pins.finalise(payload['tenant_id'],operation,committed['proof']))['state']=='finalised','real-human-receipt-pin-finalise')
        self.passed('stored_ACT_receipt_drives_real_gateway_SQL_human_source_pin_claim_ledger_and_finalise_without_AUTH04_receipt_fixture')
        decision=dict(expected_version=1,revision=1,stage=1,decision='request_information',reason='Fictional exact reason',key=str(uuid4()))
        for index in (0,1,2,3):
            denied=await browser.post('/api/v1/claims/'+cid+'/decisions',json=decision,headers=await headers(index))
            require(denied.status_code in (403,404),'noncurrent-reviewer-decision-'+str(index)+'-'+str(denied.status_code))
        decided=await browser.post('/api/v1/claims/'+cid+'/decisions',json=decision,headers=await headers(reviewer))
        require(decided.status_code==202,'actual-human-decision-'+str(decided.status_code));decision_action=decided.json();decision_op=decision_action['operation_id']
        require((await browser.post('/api/v1/claims/'+cid+'/decisions',json=decision,headers=await headers(reviewer))).json()==decision_action,'decision-idempotent-replay')
        require((await browser.post('/api/v1/claims/'+cid+'/decisions',json={**decision,'reason':'Other'},headers=await headers(reviewer))).status_code==409,'decision-reason-reconfirm')
        require((await browser.post('/api/v1/claims/'+cid+'/decisions',json={**decision,'key':str(uuid4())},headers=await headers(reviewer))).status_code==409,'one-current-claim-version-intent')
        decision_payload,actor,context,decision_row,decision_claims=await authorise(decision_op)
        require(decision_payload['data']=={k:decision[k] for k in ('revision','stage','decision','reason')},'exact-human-decision-material')
        applied=await service.execute(decision_payload,decision_row['key'],actor,context=context)
        require(applied['operation']['state']=='committed' and applied['operation']['result']['version']==2 and applied['operation']['result']['code']=='information_requested','actual-information-request-state')
        self.passed('current_reviewer_only_human_decision_binds_exact_claim_version_revision_stage_decision_reason_and_durable_replay_then_reaches_actual_service_state')
        info=await browser.post('/api/v1/drafts',json=dict(kind='information_update',trip_id=trip,claim_id=cid,conversation_id=None,key=str(uuid4())),headers=await headers())
        require(info.status_code==200,'ACT-information-draft-'+str(info.status_code));info_path='/api/v1/drafts/'+info.json()['id']
        revised=copy.deepcopy(content);revised['items'][0]['amount_minor']=550
        require((await browser.patch(info_path,json=dict(expected_version=1,key=str(uuid4()),content=revised),headers=await headers())).status_code==200,'ACT-information-edit')
        info_preview=await browser.post(info_path+'/preview',json=dict(expected_version=2),headers=await headers())
        require(info_preview.status_code==200,'ACT-information-preview')
        info_command=dict(expected_version=2,material_hash=info_preview.json()['material_hash'],key=str(uuid4()))
        info_confirm=await browser.post(info_path+'/confirm',json=info_command,headers=await headers())
        require(info_confirm.status_code==202,'ACT-information-confirm-'+str(info_confirm.status_code))
        info_action=info_confirm.json();info_payload,actor,context,info_row,_=await authorise(info_action['operation_id'])
        require(info_payload['action']=='claim.information' and info_payload['object']==dict(kind='claim',id=cid,version=2) and info_payload['data']['total_minor']==550,'ACT-information-exact-source-version-and-material')
        received=await service.execute(info_payload,info_row['key'],actor,context=context)
        require(received['operation']['state']=='committed' and received['operation']['result']['version']==3,'ACT-information-service-next-version')
        require((await pins.finalise(info_payload['tenant_id'],info_payload['operation_id'],received['proof']))['state']=='finalised','ACT-information-pin-finalise')
        self.passed('actual_information_confirmation_issues_same_claim_version_bound_human_receipt_freezes_revised_material_and_commits_next_source_revision_with_locked_policy')
        # Revoking a receipt never rewrites its immutable decision material.
        await self.execute('UPDATE {s}.human_action_receipts SET revoked=true WHERE id=%s',(decision_row['human_receipt_id'],))
        require(await resources.delegation.receipts.get(decision_row['human_receipt_id']) is None,'revoked-receipt-reader-deny')
        stamp=int(time.time())
        try:await client.verify(self.signing.sign({**decision_claims,'jti':str(uuid4()),'iat':stamp,'nbf':stamp,'exp':stamp+60}),decision_payload)
        except Exception as error:require(getattr(error,'status',None)==403,'revoked-human-receipt-gateway')
        else:raise CheckFailure('revoked-human-receipt-accepted')
        self.passed('receipt_revocation_invalidates_gateway_authority_without_mutating_original_decision_hash_or_reason')
        second,second_path,second_command=await prepare()
        async def revoked_after_source(context,trip,claim):
            evidence=await original(context,trip,claim)
            await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE id=%s',(context['session_id'],))
            return evidence
        resources.actions.drafts.read=revoked_after_source
        try:
            rejected=await browser.post(second_path+'/confirm',json=second_command,headers=await headers())
            require(rejected.status_code in (401,403,409),'confirmation-after-source-session-revoke-'+str(rejected.status_code))
        finally:resources.actions.drafts.read=original
        require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(second['id'],))=='editable','no-freeze-after-revoke')
        require(await self.scalar('SELECT count(*) FROM {s}.actions')==3,'no-action-after-revoke')
        require(await resources.delegation.receipts.get(row['human_receipt_id']) is None,'original-receipt-session-no-longer-current')
        self.people[0]['token'],self.people[0]['csrf']=await self.session(tenant,self.people[0]['person'])
        require(await resources.delegation.receipts.get(row['human_receipt_id']) is None,'fresh-session-never-renews-original-consent')
        self.passed('session_revocation_after_actual_source_read_prevents_all_confirmation_writes_and_new_login_does_not_resurrect_old_consent')
        third,third_path,third_command=await prepare()
        require((await self.mutate(self.policy_fixture['source'],'withdraw')).status_code==200,'policy-source-withdraw')
        held=await browser.post(third_path+'/confirm',json=third_command,headers=await headers())
        require(held.status_code==422,'policy-source-withdraw-before-confirm-'+str(held.status_code))
        require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(third['id'],))=='editable' and await self.scalar('SELECT count(*) FROM {s}.actions')==3,'policy-hold-no-intent-side-effects')
        self.passed('policy_source_withdrawal_between_preview_and_confirmation_holds_transaction_and_requires_current_valid_material')


def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned database fixtures.')
    folder=ROOT/'local/act01';folder.mkdir(parents=True,exist_ok=True);report={'passed':False}
    try:
        files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))
        files+=list((ROOT/'scripts/checks').glob('check_knowledge_workspace.py'))+[Path(__file__),ROOT/'scripts/checks/check_policy_eligibility.py',ROOT/'scripts/checks/check_policy_draft.py',ROOT/'scripts/checks/check_claims.py']
        hashes={str(p.resolve().relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=25,operation='bridge',direct_timeout=400))
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'workflow-source-stable')
        report.update(source_sha256=hashes,scope='actual configured platform25/reference5 human confirmation and decision receipts; policy eligibility/storage/parser/HTTP transport controlled fixtures; dispatch invoked explicitly by test, not FLOW worker; no cloud/model acceptance')
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
