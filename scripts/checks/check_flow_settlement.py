"""Owned SQL fault proof for FLOW platform settlement; terminal/ACT seed fixtures.

Configured end-to-end production authority is proved separately. This isolates
rollback and commit expiry without rerunning both application workflows.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path
import time
from uuid import UUID,uuid4
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from psycopg import sql
from psycopg.types.json import Jsonb
from check_knowledge_workspace import Harness as WorkspaceHarness,verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.integrations.security.flow_terminal import FlowTerminalVerifier
from enterprise_ai_platform.integrations.security.receipt_terminal import TerminalKey
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.modules.flows.domain import encoded,candidate
from enterprise_ai_platform.modules.jobs.ports import Lease
from enterprise_ai_platform.modules.delegation.domain import request_hash
from enterprise_ai_platform.modules.identity.service import digest


class Harness(WorkspaceHarness):
    async def settlement_boundaries(self):
        p=self.people[0];t=p['tenant'];actor=p['person'];op,key,draft,receipt=(str(uuid4()) for _ in range(4))
        uploaded=await self.upload(b'%PDF-1.4\n'+b'Owned settlement receipt '*2000,purpose='receipt',mime='application/pdf')
        require(uploaded.status_code==202,'settlement-file');await self.process()
        file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
        session=await self.scalar('SELECT id FROM {s}.sessions WHERE token_hash=%s',(digest('session',p['token']),))
        revision=await self.scalar('SELECT access_revision FROM {s}.memberships WHERE id=%s',(p['member'],))
        item=dict(id=str(uuid4()),trip_item_id=str(uuid4()),amount_minor=500,receipt_ids=[file['id']]);refs=[dict(id=file['id'],version=file['version'],digest=file['digest'])]
        data=dict(trip_id=str(uuid4()),currency='SGD',items=[item],total_minor=500,receipts=refs,policy=dict(id=str(uuid4()),version=1,digest='a'*64),approval_route=[dict(stage=1,approver_id=str(uuid4()),scope_ref='reimbursement')],destination='reference-reimbursement')
        payload=dict(schema_version=1,action='claim.submit',tenant_id=str(t),actor_id=str(actor),operation_id=op,object=dict(kind='draft',id=draft,version=1),data=data)
        canonical=request_hash(payload);identity=dict(tenant_id=str(t),operation_id=op,actor_id=str(actor),caller='platform-dispatcher',kind='claim.submit',key=key,request_hash=canonical,object_kind='draft',object_id=draft,object_version=1)
        batch=dict(identity=identity,claim_id=candidate(identity),receipts=refs);caller_hash='e'*64;secret=b's'*32
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'flow-settlement',%s,'reference-reimbursement','platform-dispatcher',%s,clock_timestamp()+interval '1 hour')",(uuid4(),caller_hash,Jsonb(['claim.submit'])))
        await self.execute("INSERT INTO {s}.receipt_receiver_keys VALUES(%s,'fixture',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(caller_hash,secret))
        epoch=await self.scalar('SELECT floor(extract(epoch FROM clock_timestamp()))::bigint')
        async with self.admin.transaction():
            await self.execute("INSERT INTO {s}.drafts(tenant_id,id,owner_principal_id,kind,trip_ref,state) VALUES(%s,%s,%s,'submission',%s,'frozen')",(t,draft,actor,data['trip_id']))
            await self.execute('INSERT INTO {s}.draft_versions VALUES(%s,%s,1,%s,%s,clock_timestamp())',(t,draft,Jsonb(dict(currency='SGD',items=[item],receipts=[dict(id=file['id'],version=file['version'])])),'a'*64))
            await self.execute("INSERT INTO {s}.human_action_receipts VALUES(%s,%s,%s,%s,%s,'claim.submit','draft',%s,1,%s,%s,false)",(receipt,t,actor,session,op,draft,canonical,epoch))
            await self.execute("INSERT INTO {s}.actions(tenant_id,id,actor_id,membership_id,session_id,access_revision,kind,key,request_identity_hash,draft_id,draft_version,material_hash,human_receipt_id,payload,canonical_hash,confirmation_expires_at) VALUES(%s,%s,%s,%s,%s,%s,'claim.submit',%s,%s,%s,1,%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",(t,op,actor,p['member'],session,revision,key,'a'*64,draft,'a'*64,receipt,Jsonb(payload),canonical))
            await self.execute("INSERT INTO {s}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision) VALUES(%s,%s,'action.dispatch',%s,%s,1,%s,%s,%s,%s)",(t,op,op,op,actor,p['member'],session,revision))
            await self.execute('INSERT INTO {s}.job_tenants(tenant_id) VALUES(%s) ON CONFLICT DO NOTHING',(t,))
            await self.execute("INSERT INTO {s}.receipt_pin_batches(tenant_id,operation_id,actor_id,candidate_claim_id,caller_ref,batch,batch_digest,authority_expires_at) VALUES(%s,%s,%s,%s,'flow-settlement',%s,%s,clock_timestamp()+interval '1 hour')",(t,op,actor,candidate(identity),Jsonb(batch),hashlib.sha256(b'eai:receipt-pin-batch:v1:'+encoded(batch)).hexdigest()))
            await self.execute('INSERT INTO {s}.receipt_pins VALUES(%s,%s,%s,%s,%s)',(t,op,file['id'],file['version'],file['digest']))
        picked=await self.scheduler.claim();require(picked['id']==op,'settlement-job');lease=Lease(UUID(op),t,picked['generation'])
        common=dict(credential_hash=self.jobs._credential_hash,id=op,generation=lease.generation)
        async def call(operation,extra=None):
            async with self.database.transaction() as connection:
                return (await (await connection.execute(sql.SQL('SELECT {}.flow_call(%s,%s)').format(sql.Identifier(self.schema)),(operation,Jsonb({**common,**(extra or {})})))).fetchone())[0]
        require((await call('prepare'))['mode']=='dispatch','settlement-prepared')
        private=Ed25519PrivateKey.generate();public=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        result=dict(ref=candidate(identity),version=1,code='claim_submitted')
        terminal=dict(identity=identity,state='committed',result=result,result_digest=hashlib.sha256(b'reimbursement:operation-result:v1:'+encoded(result)).hexdigest(),completed_at=epoch,ledger_revision=1)
        verifier=FlowTerminalVerifier({'fixture':TerminalKey(public,epoch-60,epoch+3600)},SettlementKey('fixture',secret,epoch-60,epoch+3600),caller_hash)
        def evidence(expires=None):
            stamp=max(int(time.time()),epoch);body=dict(schema_version=1,issuer='reference-reimbursement',audience='enterprise-ai-platform',kid='fixture',operation=terminal,issued_at=stamp,expires_at=expires or stamp+30)
            proof=dict(payload=body,signature=base64.urlsafe_b64encode(private.sign(b'reimbursement:terminal-proof:v1:'+encoded(body))).decode().rstrip('='))
            verified=verifier.verify(dict(operation=terminal,proof=proof),identity);flow,pins=verifier.settlement(verified,identity,batch)
            return dict(settlement=flow,receipt_settlement=pins)
        async def unchanged():
            require(await self.scalar('SELECT state FROM {s}.actions WHERE id=%s',(op,))=='sending','rollback-action')
            require(await self.scalar('SELECT state FROM {s}.drafts WHERE id=%s',(draft,))=='frozen','rollback-draft')
            require(await self.scalar('SELECT state FROM {s}.receipt_pin_batches WHERE operation_id=%s',(op,))=='held','rollback-pins')
            require(await self.scalar('SELECT state FROM {s}.outbox_jobs WHERE id=%s',(op,))=='running','rollback-job')
            require(await self.scalar('SELECT count(*) FROM {s}.flow_results WHERE operation_id=%s',(op,))==0,'rollback-result')
        await self.execute("CREATE FUNCTION {s}._flow_fixture_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.state='completed' THEN RAISE EXCEPTION 'Owned fixture failure' USING ERRCODE='23514';END IF;RETURN NEW;END $$")
        await self.execute('REVOKE ALL ON FUNCTION {s}._flow_fixture_fail() FROM PUBLIC,{a}')
        await self.execute('CREATE TRIGGER flow_fixture_fail BEFORE UPDATE ON {s}.outbox_jobs FOR EACH ROW EXECUTE FUNCTION {s}._flow_fixture_fail()')
        try:require((await call('settle',evidence()))['status']=='invalid','injected-final-job-write-failed')
        finally:await self.execute('DROP TRIGGER flow_fixture_fail ON {s}.outbox_jobs');await self.execute('DROP FUNCTION {s}._flow_fixture_fail()')
        await unchanged();self.passed('failure_at_final_job_completion_rolls_back_pin_finalise_action_draft_and_terminal_result_together')
        await self.jobs.heartbeat(lease)
        await self.execute('CREATE FUNCTION {s}._flow_fixture_pause() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(3);RETURN NULL;END $$')
        await self.execute('REVOKE ALL ON FUNCTION {s}._flow_fixture_pause() FROM PUBLIC,{a}')
        await self.execute('CREATE CONSTRAINT TRIGGER aa_flow_fixture_pause AFTER INSERT ON {s}.flow_results DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {s}._flow_fixture_pause()')
        returned=False
        try:
            short=await self.scalar('SELECT floor(extract(epoch FROM clock_timestamp()))::bigint+2')
            async with self.database.transaction() as connection:
                value=(await (await connection.execute(sql.SQL("SELECT {}.flow_call('settle',%s)").format(sql.Identifier(self.schema)),(Jsonb({**common,**evidence(short)}),))).fetchone())[0]
                require(value['status']=='ok','short-proof-valid-before-commit');returned=True
            raise CheckFailure('expired-terminal-commit-accepted')
        except Exception as error:
            codes=[];current=error
            for _ in range(8):
                if current is None:break
                codes.append(getattr(current,'sqlstate',None));current=current.__context__
            require(returned and '40001' in codes,'deferred-flow-expiry-rollback')
        finally:await self.execute('DROP TRIGGER aa_flow_fixture_pause ON {s}.flow_results');await self.execute('DROP FUNCTION {s}._flow_fixture_pause()')
        await unchanged();self.passed('terminal_proof_expiry_after_RPC_return_at_deferred_commit_rolls_back_all_platform_settlement_effects')
        await self.jobs.heartbeat(lease);require((await call('settle',evidence()))['state']=='committed','reissued-proof-success')
        for table,key_column,state in (('actions','id','committed'),('drafts','id','committed'),('receipt_pin_batches','operation_id','finalised'),('outbox_jobs','id','completed')):
            require(await self.scalar('SELECT state FROM {s}.'+table+' WHERE '+key_column+'=%s',(draft if table=='drafts' else op,))==state,'atomic-retry-'+table)
        require(await self.scalar('SELECT count(*) FROM {s}.flow_results WHERE operation_id=%s',(op,))==1,'one-result-after-retry')
        self.passed('freshly_reissued_same_immutable_terminal_retries_safely_and_commits_exactly_one_platform_result')


def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned fixture.')
    try:
        settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=26,operation='settlement_boundaries'))
        result['scope']='restricted platform settlement fault/expiry proof; owned ACT/pin seed and Ed25519 terminal fixture, actual SQL lease/crypto/MAC/transaction; not duplicate end-to-end source acceptance'
        result['helper_sha256']={Path(__file__).as_posix():hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        write_json(Path('local/flow01/settlement-proof.json'),result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','helper_sha256')}))
    except Exception as error:print('Flow settlement proof failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None


if __name__=='__main__':main()
