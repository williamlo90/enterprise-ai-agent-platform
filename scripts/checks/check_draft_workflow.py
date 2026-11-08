"""DRAFT-01 owned DB proof; source facts/MAC producer is a controlled fixture.

Actual restricted SQL, sessions, published policy, files and draft APIs. This is
not a source HTTP producer, ACT consent, cloud or model-quality acceptance.
"""
import argparse
import asyncio
import copy
from datetime import datetime,timedelta
import hashlib
import hmac
import json
import logging
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import UUID,uuid4
from zoneinfo import ZoneInfo
import httpx
from psycopg import sql,Error as DatabaseError
from psycopg.types.json import Jsonb
from check_policy_draft import Harness as PolicyHarness
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.drafts import PostgresDraftRepository
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.identity.service import IdentityService,digest
from enterprise_ai_platform.modules.drafts.domain import VerifiedDraftSource,encoded,material_hash
from enterprise_ai_platform.modules.drafts.service import DraftService


class Harness(PolicyHarness):
    async def draft_workflow(self):
        try:await self._draft_workflow()
        except CheckFailure:raise
        except Exception as error:
            import traceback
            frames=traceback.extract_tb(error.__traceback__)
            where=','.join(Path(f.filename).name+':'+str(f.lineno) for f in frames[-4:])
            raise CheckFailure(type(error).__name__+'@'+where) from None

    async def _draft_workflow(self):
        await self.policy_kernel()
        person=self.people[0];tenant=person['tenant'];actor=person['person']
        self.secret=b'd'*32;self.credential='d'*64
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'draft-fixture',%s,'reference-reimbursement','platform-dispatcher',%s,clock_timestamp()+interval '1 hour')",(uuid4(),self.credential,Jsonb(['trip.read','claim.read'])))
        await self.execute("INSERT INTO {s}.receipt_receiver_keys VALUES(%s,'draft',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(self.credential,self.secret))
        trip_id=str(uuid4());item_id=str(uuid4());day=datetime.now(ZoneInfo(self.policy_fixture['content']['timezone'])).date().isoformat()
        self.trip=dict(id=trip_id,start_date=day,end_date=day,state='completed',employee_ref='Fictional employee',items=[dict(id=item_id,expense_date=day,category='travel',declared_amount_minor=1000,currency='SGD')])
        for route in self.policy_fixture['content']['rules']['approval_route']:
            await self.member(tenant,UUID(route['approver_id']),grants=(RoleGrant('approver','process','reimbursement'),))
        repo=PostgresDraftRepository(self.database)
        class Sources:
            async def read(inner,context,trip,claim):return self.source(context,claim)
        service=DraftService(repo,ORIGIN,Sources())
        identity=IdentityService(SimpleNamespace(public_origin=ORIGIN),PostgresIdentityRepository(self.database),None,None)
        settings=Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True})
        app=create_app(settings,services=ApplicationServices(health=SimpleNamespace(),identity=identity,drafts=service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
            headers={**self.headers(),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}
            boot=await client.get('/api/v1/me',headers=headers);require(boot.status_code==200,'draft-bootstrap')
            person['csrf']=boot.json()['csrf_token'];headers=self.headers(mime='application/json');headers['x-eai-expected-context']=boot.json()['context_binding']
            command=dict(kind='submission',trip_id=trip_id,claim_id=None,conversation_id=None,key=str(uuid4()))
            # Restricted raw call exposes only SQLSTATE/function line when diagnosing.
            initial=await self.rpc('create',{**command,'source':None,'csrf_hash':digest('csrf',person['csrf'])})
            require(initial.get('status')=='ok' and initial['draft'] is None,'initial-source-context-'+str(initial.get('status')))
            source=self.source(initial['context'],None).wire();candidate=str(uuid4())
            created=await self.rpc('create',{**command,'source':source,'id':candidate,'csrf_hash':digest('csrf',person['csrf'])})
            require(created.get('status')=='ok','draft-create-'+str(created.get('status')))
            self.passed('restricted_current_session_creates_empty_owner_draft_with_private_bound_source_evidence')
            path='/api/v1/drafts/'+candidate
            read=await client.get(path,headers=headers);require(read.status_code==200 and read.json()['version']==1,'draft-read-'+str(read.status_code))
            replay=await client.post('/api/v1/drafts',json=command,headers=headers)
            require(replay.status_code==200 and replay.json()['id']==candidate,'draft-create-replay')
            other={**command,'trip_id':str(uuid4())}
            require((await client.post('/api/v1/drafts',json=other,headers=headers)).status_code==409,'draft-key-material-conflict')
            self.passed('actual_HTTP_create_replay_and_read_preserve_one_draft_and_reject_reused_key_with_other_material')
            for index in (1,2,3,4):
                foreign_headers={**self.headers(index),'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'}
                foreign_boot=await client.get('/api/v1/me',headers=foreign_headers)
                require(foreign_boot.status_code==200,'foreign-bootstrap')
                foreign_headers=self.headers(index);foreign_headers['x-eai-expected-context']=foreign_boot.json()['context_binding']
                foreign=await client.get(path,headers=foreign_headers)
                require(foreign.status_code in (403,404,409),'draft-foreign-owner-'+str(foreign.status_code))
            require((await client.patch(path,json={},headers={**headers,'origin':'https://foreign.example.test'})).status_code in (400,403),'draft-origin')
            self.passed('other_owner_foreign_tenant_admin_and_reader_do_not_gain_draft_access_and_mutation_requires_origin')
            incomplete=await self.rpc('preview',dict(id=candidate,expected_version=1,source=self.source(initial['context'],None).wire(),csrf_hash=digest('csrf',person['csrf'])))
            require(incomplete.get('status')=='incomplete','empty-preview-'+str(incomplete.get('status')))
            uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional draft receipt '*2000,purpose='receipt',mime='application/pdf')
            require(uploaded.status_code==202,'draft-receipt-upload');await self.process()
            file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
            content=dict(currency='SGD',items=[dict(id=str(uuid4()),trip_item_id=item_id,amount_minor=500,receipt_ids=[file['id']])],receipts=[dict(id=file['id'],version=file['version'])])
            change=dict(expected_version=1,key=str(uuid4()),content=content)
            edited=await client.patch(path,json=change,headers=headers)
            require(edited.status_code==200 and edited.json()['version']==2,'draft-edit-'+str(edited.status_code))
            require((await client.patch(path,json=change,headers=headers)).json()['version']==2,'edit-replay')
            stale={**change,'key':str(uuid4())}
            require((await client.patch(path,json=stale,headers=headers)).status_code==409,'edit-stale')
            self.passed('empty_preview_is_incomplete_and_edit_uses_immutable_version_CAS_with_idempotent_replay')
            args=dict(id=candidate,expected_version=2,source=self.source(initial['context'],None).wire(),csrf_hash=digest('csrf',person['csrf']))
            actual=await self.rpc('preview',args);require(actual.get('status')=='ok','preview-'+str(actual.get('status')))
            preview=actual['preview'];material={k:v for k,v in preview.items() if k not in ('preview','material_hash','declared_amounts','requires_confirmation')}
            require(material_hash(material)==preview['material_hash'],'preview-independent-hash')
            response=await client.post(path+'/preview',json=dict(expected_version=2),headers=headers)
            require(response.status_code==200 and response.json()['material_hash']==preview['material_hash'],'preview-http-'+str(response.status_code))
            require(preview['requires_confirmation'] and preview['declared_amounts'] and preview['data']['total_minor']==500,'preview-declared-money')
            require(preview['data']['policy']==self.policy_fixture['policy'] and preview['data']['receipts'][0]['digest']==file['digest'],'preview-current-policy-and-file')
            require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(candidate,))=='editable','preview-not-consent')
            require(await self.scalar('SELECT count(*) FROM {s}.draft_previews WHERE draft_id=%s',(candidate,))==1,'stable-preview-material')
            self.passed('actual_HTTP_preview_matches_independent_canonical_hash_current_policy_route_and_receipt_without_freezing_or_consent')
            corrupt=copy.deepcopy(args);corrupt['source']['material']['trip']['items'][0]['category']='other'
            require((await self.rpc('preview',corrupt)).get('status')=='unavailable','source-mac-forgery')
            await self.execute("UPDATE {s}.receipt_receiver_keys SET active=false WHERE key_id='draft'")
            require((await self.rpc('preview',args)).get('status')=='unavailable','source-key-revoke')
            await self.execute("UPDATE {s}.receipt_receiver_keys SET active=true WHERE key_id='draft'")
            self.passed('browser_cannot_forge_source_facts_and_current_source_key_revocation_rejects_old_evidence')
            original_trip=copy.deepcopy(self.trip)
            old=(datetime.fromisoformat(day)-timedelta(days=15)).date().isoformat()
            self.trip.update(start_date=old,end_date=old)
            expired={**args,'source':self.source(initial['context'],None).wire()}
            require((await self.rpc('preview',expired)).get('status')=='incomplete','policy-calendar-deadline')
            self.trip=original_trip
            self.passed('submission_preview_enforces_policy_calendar_deadline_using_authoritative_trip_end_and_timezone')
            await self.execute("UPDATE {s}.files SET deletion_state='deletion_pending' WHERE id=%s",(file['id'],))
            require((await self.rpc('preview',args)).get('status')=='incomplete','receipt-revoke')
            await self.execute("UPDATE {s}.files SET deletion_state='active' WHERE id=%s",(file['id'],))
            route=self.policy_fixture['content']['rules']['approval_route'][0]['approver_id']
            await self.execute('UPDATE {s}.memberships SET active=false WHERE tenant_id=%s AND principal_id=%s',(tenant,route))
            require((await self.rpc('preview',args)).get('status')=='incomplete','reviewer-revoke')
            await self.execute('UPDATE {s}.memberships SET active=true WHERE tenant_id=%s AND principal_id=%s',(tenant,route))
            self.passed('receipt_deletion_and_current_approver_deactivation_invalidate_preview_without_changing_saved_draft')
            wrong=dict(id=candidate,expected_version=2,content={**content,'items':[{**content['items'][0],'trip_item_id':str(uuid4())}]},key=str(uuid4()),csrf_hash=digest('csrf',person['csrf']))
            require((await self.rpc('replace',wrong)).get('status')=='ok','editable-invalid-source-item-save')
            require((await self.rpc('preview',{**args,'expected_version':3})).get('status')=='incomplete','unknown-source-item-preview')
            self.passed('editable_draft_can_save_incomplete_business_data_but_preview_rejects_items_outside_authoritative_trip')
        async with self.database.transaction() as connection:
            for table in ('drafts','draft_versions','draft_requests','draft_previews'):
                row=await (await connection.execute("SELECT has_table_privilege(current_user,%s,'SELECT,INSERT,UPDATE,DELETE')",(self.schema+'.'+table,))).fetchone()
                require(row==(False,),'draft-private-'+table)
        self.passed('restricted_runtime_has_only_draft_RPC_and_no_direct_private_draft_table_access')
        # Commit-time FK integrity: header must point to a version of itself.
        for wrong_parent in (False,True):
            new=str(uuid4())
            try:
                async with self.admin.transaction():
                    await self.execute("INSERT INTO {s}.drafts(tenant_id,id,owner_principal_id,kind,trip_ref) VALUES(%s,%s,%s,'submission',%s)",(tenant,new,actor,trip_id))
                    if wrong_parent:
                        await self.execute("INSERT INTO {s}.draft_versions VALUES(%s,%s,1,%s,%s,clock_timestamp())",(tenant,str(uuid4()),Jsonb(dict(currency='SGD',items=[],receipts=[])),'a'*64))
                raise CheckFailure('draft-deferred-FK-accepted')
            except DatabaseError as error:require(error.sqlstate=='23503','draft-deferred-FK-sqlstate')
            require(await self.scalar('SELECT count(*) FROM {s}.drafts WHERE id=%s',(new,))==0,'draft-deferred-rollback')
        self.passed('deferred_header_current_version_and_wrong_parent_FKs_fail_at_commit_and_roll_back_orphan_drafts')
        try:
            async with self.admin.transaction():
                await self.execute("INSERT INTO {s}.draft_previews VALUES(%s,%s,3,%s,%s,%s,clock_timestamp()-interval '1 second',clock_timestamp())",(tenant,candidate,'f'*64,Jsonb({}),'a'*64))
            raise CheckFailure('expired-preview-commit-accepted')
        except DatabaseError as error:require(error.sqlstate=='40001','preview-deferred-expiry')
        require(await self.scalar("SELECT count(*) FROM {s}.draft_previews WHERE material_hash=%s",('f'*64,))==0,'expired-preview-rolled-back')
        try:
            async with self.admin.transaction():
                await self.admin.execute(sql.SQL("UPDATE {}.draft_versions SET content_hash=%s WHERE draft_id=%s").format(sql.Identifier(self.schema)),('f'*64,candidate))
            raise CheckFailure('immutable-version-update-accepted')
        except DatabaseError as error:require(error.sqlstate=='23514','immutable-version-update-guard')
        self.passed('preview_expiry_is_rechecked_at_commit_and_saved_draft_version_history_cannot_be_rewritten')

    def source(self,context,claim):
        identity={k:context[k] for k in ('tenant_id','actor_id','membership_id','session_id','access_revision')}
        now=int(time.time());material=dict(schema_version=1,context=identity,trip=self.trip,claim=claim,checked_at=now,expires_at=now+55)
        proof=hmac.new(self.secret,b'eai:draft-source:v1:'+encoded(material),hashlib.sha256).hexdigest()
        return VerifiedDraftSource(encoded(dict(credential_hash=self.credential,key_id='draft',material=material,proof=proof)))

    async def rpc(self,operation,values):
        async with self.database.transaction() as connection:
            try:return (await (await connection.execute(sql.SQL('SELECT {}.draft_call(%s,%s,%s)').format(sql.Identifier(self.schema)),(digest('session',self.people[0]['token']),operation,Jsonb(values)))).fetchone())[0]
            except DatabaseError as error:
                # SQLSTATE + internal source line, never SQL arguments/query text.
                import re
                line=re.findall(r'line (\d+)',getattr(error.diag,'context','') or '')
                raise CheckFailure('draft-rpc-'+str(error.sqlstate)+'-lines-'+','.join(line)) from None


def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/draft01/kernel-proof.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned DB fixtures.')
    try:
        own={Path(__file__).as_posix():hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=24,operation='draft_workflow'))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in own.items()),'proof-helper-stable')
        result.update(scope='draft_SQL_API_with_controlled_source_producer_not_ACT_or_source_HTTP_acceptance',helper_sha256=own)
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result)
        print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','helper_sha256')}))
    except Exception as error:print('Draft proof failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None


if __name__=='__main__':main()
