"""POL-01 draft SQL kernel proof in an owned schema; no installed schema upgrade."""
import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from uuid import uuid4
from psycopg import sql
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from check_knowledge_workspace import Harness as WorkspaceHarness,verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import access_role,revisions
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.modules.policies.domain import encoded
from enterprise_ai_platform.modules.identity.service import digest


DRAFTS=[Path('backend/src/enterprise_ai_platform/infrastructure/database/migrations/0021_policies.sql')]


class RpcFailure(CheckFailure):
    def __init__(self,error):
        self.sqlstate=error.sqlstate
        super().__init__('policy-rpc-'+str(self.sqlstate))


def manifest():
    source=Path('backend/src/enterprise_ai_platform/infrastructure/database/migrations/0020_knowledge_audience.sql').read_text(encoding='utf-8')
    source=source[source.index('CREATE OR REPLACE FUNCTION {schema}._runtime_role_is_valid'):]
    private="'delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys',"
    source=source.replace("'sessions','login_transactions'",private+"'sessions','login_transactions'")
    marker="AND NOT (f.proname='organization_call'"
    source=source.replace(marker,"AND NOT (f.proname='trip_catalog_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text')\n                        AND NOT (f.proname='policy_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')\n                        AND NOT (f.proname='policy_eligibility_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='auth jsonb, request jsonb')\n                        "+marker)
    return source


class Harness(WorkspaceHarness):
    async def policy_kernel(self):
        try:
            installed=await self.scalar('SELECT checksum FROM {s}.schema_migrations WHERE version=21')
            require(installed==revisions()[20].checksum,'packaged-policy-migration-checksum')
            self.passed('packaged_schema21_capabilities_and_private_role_manifest_installed_in_disposable_schema')
            async with self.database.transaction() as connection:
                valid=await (await connection.execute(sql.SQL('SELECT {}.runtime_role_is_valid()').format(sql.Identifier(self.schema)))).fetchone()
                require(valid==(True,),'policy-runtime-manifest')
                for name in ('policy_receiver_keys','delegation_verification_contexts','policy_packages','policy_dispatches'):
                    row=await (await connection.execute('SELECT has_table_privilege(current_user,%s,\'SELECT,INSERT,UPDATE,DELETE\')',(self.schema+'.'+name,))).fetchone()
                    require(row==(False,),'private-'+name)
            self.passed('restricted_runtime_accepts_exact_policy_capabilities_but_cannot_read_or_write_private_state')
            for secret,message in [(b'k'*32,b'bound intent'),(b'x'*80,b'long key'),(b'',b'empty key')]:
                row=await self.scalar('SELECT encode({s}._policy_hmac(%s,%s),\'hex\')',(secret,message))
                require(row==hmac.new(secret,message,hashlib.sha256).hexdigest(),'hmac-independent-vector')
            self.passed('sql_hmac_matches_python_independent_short_long_and_empty_key_vectors')
            tenant,package=str(uuid4()),str(uuid4())
            content=dict(tenant_id=tenant,package_id=package,package_version=1,schema_version=1,
                valid_trip_end_from='2026-01-01',valid_trip_end_to=None,timezone='Asia/Singapore',
                document_versions=[dict(document_id=str(uuid4()),version_id=str(uuid4()),content_digest='a'*64)],
                rules=dict(currency='SGD',submission_deadline_days=7,max_total_minor=None,
                    categories=[dict(code='travel',receipt_required=True,max_amount_minor=None)],
                    approval_route=[dict(stage=1,approver_id=str(uuid4()),scope_ref='reimbursement')]))
            async def accepts(value):
                return await self.scalar('SELECT {s}._policy_content_valid(%s,%s,%s,1)',(Jsonb(value),tenant,package))
            require(await accepts(content) is True,'valid-content')
            canonical=await self.scalar('SELECT {s}._policy_canonical(%s)',(Jsonb(content),))
            require(canonical.encode()==encoded(content),'canonical-parity')
            mutations=[(('valid_trip_end_from',),None),(('valid_trip_end_to',),5),(('timezone',),None),
                (('package_version',),'1'),(('package_version',),1.0),(('schema_version',),1.0),
                (('rules','submission_deadline_days'),'7'),(('rules','submission_deadline_days'),None),
                (('rules','max_total_minor'),'20'),(('rules','categories',0,'max_amount_minor'),'20'),
                (('rules','categories',0,'code'),None),(('rules','approval_route',0,'stage'),'1'),
                (('rules','approval_route',0,'approver_id'),None),(('document_versions',0,'content_digest'),None)]
            for path,value in mutations:
                changed=copy.deepcopy(content);parent=changed
                for key in path[:-1]:parent=parent[key]
                parent[path[-1]]=value
                require(await accepts(changed) is False,'content-type-'+'.'.join(map(str,path)))
            self.passed('canonical_content_matches_python_and_rejects_fourteen_invalid_scalar_or_numeric_types')
            person=self.people[0];tenant=person['tenant'];actor=person['person']
            await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(%s,%s,%s,'process_owner','process','reimbursement',%s)",
                               (tenant,uuid4(),person['member'],actor))
            # The role-change trigger revises membership and invalidates old sessions.
            person['token'],person['csrf']=await self.session(tenant,actor)
            source=await self.document('Fictional approved reimbursement policy.')
            published=await self.mutate(source,'publish');require(published.status_code==200,'source-published')
            content['tenant_id']=str(tenant)
            content['timezone']=await self.scalar('SELECT timezone FROM {s}.tenants WHERE id=%s',(tenant,))
            content['document_versions']=[dict(document_id=source['document_id'],version_id=source['version_id'],content_digest=await self.scalar('SELECT content_digest FROM {s}.document_versions WHERE id=%s',(source['version_id'],)))]
            if getattr(self,'fixture_approval_route',None) is not None:content['rules']['approval_route']=self.fixture_approval_route
            raw=encoded(content);package_digest=hashlib.sha256(b'reimbursement:rule-package:v1:'+raw).hexdigest()
            async def call(operation,values):
                async with self.database.transaction() as connection:
                    try:
                        return (await (await connection.execute(sql.SQL('SELECT {}.policy_call(%s,%s,%s)').format(sql.Identifier(self.schema)),
                            (digest('session',person['token']),operation,Jsonb({**values,'csrf_hash':digest('csrf',person['csrf'])})))).fetchone())[0]
                    except DatabaseError as error:raise RpcFailure(error) from None
            def command(version,**extra):return dict(package_id=package,package_version=1,key=str(uuid4()),expected_version=version,**extra)
            draft=await call('draft',command(0,canonical=raw.decode(),digest=package_digest));require(draft.get('state')=='draft','policy-draft-'+str(draft.get('status')))
            prepared=await call('prepare',command(1));require(prepared.get('state')=='prepared' and prepared.get('version')==2,'policy-prepare-'+str(prepared.get('status')))
            require((await call('activate',command(2))).get('status')=='conflict','unacknowledged-cannot-activate')
            intent_command=command(2);intent=await call('dispatch',intent_command);require(intent.get('status')=='ok','policy-dispatch-'+str(intent.get('status')))
            require((await call('dispatch',intent_command)).get('operation_id')==intent['operation_id'],'dispatch-stable-intent')
            received=dict(package_id=package,package_version=1,digest=package_digest,received_at=int(time.time()),state='received')
            ack_command={**intent_command,'ack':received}
            require((await call('ack',ack_command)).get('status')=='invalid','own-browser-cannot-fabricate-ack-without-proof')
            ack_command['settlement']=dict(key_id='fixture',proof='0'*64)
            require((await call('ack',ack_command)).get('status')=='forbidden','unknown-receiver-denied')
            secret=hashlib.sha256(b'eai:policy-settlement:key:v1:'+b'f'*43).digest()
            await self.execute("INSERT INTO {s}.policy_receiver_keys(key_id,key_hash,not_before,not_after) VALUES('fixture',%s,clock_timestamp()-interval '1 minute',clock_timestamp()+interval '1 hour')",(secret.hex(),))
            require((await call('ack',ack_command)).get('status')=='forbidden','own-browser-wrong-proof-denied')
            proof_material=dict(tenant_id=str(tenant),actor_id=str(actor),package_id=package,package_version=1,key=intent_command['key'],operation_id=intent['operation_id'],expected_version=2,ack=received)
            ack_command['settlement']['proof']=hmac.new(secret,encoded(proof_material),hashlib.sha256).hexdigest()
            changed={**ack_command,'ack':{**received,'received_at':received['received_at']-1}}
            require((await call('ack',changed)).get('status')=='forbidden','cross-material-proof-denied')
            await self.execute("UPDATE {s}.policy_receiver_keys SET active=false WHERE key_id='fixture'")
            require((await call('ack',ack_command)).get('status')=='forbidden','revoked-receiver-denied')
            require(await self.scalar('SELECT service_ack_digest IS NULL FROM {s}.policy_packages WHERE id=%s',(package,)) is True,'negative-proof-no-ack-state')
            self.passed('direct_restricted_publisher_with_own_session_csrf_and_intent_cannot_settle_missing_wrong_cross_material_or_revoked_ack_proof')
            await self.execute("UPDATE {s}.policy_receiver_keys SET active=true WHERE key_id='fixture'")
            # Owned-fixture fault injection only: delay the final authorization
            # after successful gate validation/writes, across receiver expiry.
            await self.execute('ALTER FUNCTION {s}.authorization_call(text,text,jsonb) RENAME TO _policy_fixture_authorization')
            await self.admin.execute(sql.SQL('REVOKE ALL ON FUNCTION {}._policy_fixture_authorization(text,text,jsonb) FROM PUBLIC,{}').format(sql.Identifier(self.schema),sql.Identifier(access_role(self.schema))))
            try:
                await self.execute("""CREATE FUNCTION {s}.authorization_call(token text,operation text,payload jsonb) RETURNS jsonb
                  LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$ BEGIN
                    IF operation='finish' THEN PERFORM pg_sleep(3); END IF;
                    RETURN {s}._policy_fixture_authorization(token,operation,payload);
                  END; $$""")
                await self.admin.execute(sql.SQL('REVOKE ALL ON FUNCTION {}.authorization_call(text,text,jsonb) FROM PUBLIC; GRANT EXECUTE ON FUNCTION {}.authorization_call(text,text,jsonb) TO {}').format(sql.Identifier(self.schema),sql.Identifier(self.schema),sql.Identifier(access_role(self.schema))))
                await self.execute("UPDATE {s}.policy_receiver_keys SET not_after=clock_timestamp()+interval '2 seconds' WHERE key_id='fixture'")
                try:
                    await call('ack',ack_command)
                    raise CheckFailure('expired-receiver-settlement-did-not-rollback')
                except Exception as expired:
                    code=getattr(expired,'sqlstate',None)
                    require(code=='40001','receiver-expiry-rollback-sqlstate-'+(code if isinstance(code,str) else type(expired).__name__))
            finally:
                await self.execute('DROP FUNCTION {s}.authorization_call(text,text,jsonb)')
                await self.execute('ALTER FUNCTION {s}._policy_fixture_authorization(text,text,jsonb) RENAME TO authorization_call')
                await self.admin.execute(sql.SQL('GRANT EXECUTE ON FUNCTION {}.authorization_call(text,text,jsonb) TO {}').format(sql.Identifier(self.schema),sql.Identifier(access_role(self.schema))))
            require(await self.scalar('SELECT service_ack_digest IS NULL FROM {s}.policy_packages WHERE id=%s',(package,)) is True,'receiver-expiry-no-package-ack')
            require(await self.scalar('SELECT ack IS NULL FROM {s}.policy_dispatches WHERE key=%s',(intent_command['key'],)) is True,'receiver-expiry-no-intent-ack')
            require(await self.scalar("SELECT count(*) FROM {s}.policy_audit WHERE package_id=%s AND operation='ack'",(package,))==0,'receiver-expiry-no-ack-audit')
            self.passed('receiver_key_expiry_during_final_authorization_rolls_back_package_intent_and_ack_audit')
            await self.execute("UPDATE {s}.policy_receiver_keys SET not_after=clock_timestamp()+interval '1 hour' WHERE key_id='fixture'")
            acknowledged=await call('ack',ack_command)
            require(acknowledged.get('status')=='ok' and acknowledged.get('state')=='prepared' and acknowledged.get('acknowledged') is True,'server-proof-ack-without-activation')
            require((await call('ack',ack_command)).get('version')==3,'same-ack-replay-no-version-bump')
            active=await call('activate',command(3));require(active.get('state')=='active' and active.get('allowed_for_new') is True,'explicit-activation-'+str(active.get('status')))
            self.passed('draft_prepare_durable_dispatch_server_proof_ack_and_explicit_activation_work_through_restricted_runtime_capability')
            self.policy_fixture=dict(policy=dict(id=package,version=1,digest=package_digest),source=source,content=content)
        except Exception as error:
            if isinstance(error,CheckFailure):raise
            code=getattr(error,'sqlstate',None)
            position=getattr(getattr(error,'diag',None),'statement_position',None)
            location=''
            if position and str(position).isdigit():
                rendered=sql.SQL(source).format(schema=sql.Identifier(self.schema),access_role=sql.Identifier(access_role(self.schema))).as_string(self.admin)
                location='-line-'+str(rendered[:int(position)-1].count('\n')+1)
            raise CheckFailure('policy-kernel-'+(code if isinstance(code,str) and len(code)==5 else type(error).__name__)+location) from None


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/pol01/draft-kernel-proof.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned disposable database proof.')
    try:
        snapshots={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in DRAFTS+[Path(__file__)]}
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,harness_type=Harness,target_version=21,operation='policy_kernel'))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in snapshots.items()),'draft-source-stable')
        result.update(scope='draft_kernel_only_not_policy_acceptance',draft_sha256=snapshots)
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result)
        print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','draft_sha256')}))
    except Exception as error:
        print('Policy draft proof failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None


if __name__=='__main__':main()
