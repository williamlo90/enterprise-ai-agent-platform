"""Serial REF-03 real PostgreSQL races and atomic fixture effects, no platform import."""
import argparse
import asyncio
import base64
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import secrets
import time
from uuid import uuid4
import httpx
from psycopg import sql
from dotenv import dotenv_values
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from reimbursement_service.composition import database_loop_factory
from reimbursement_service.config import Settings
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase,direct_connection,Limits
from reimbursement_service.infrastructure.database.migration import migrate,revisions
from reimbursement_service.infrastructure.database.operations import PostgresOperations
from reimbursement_service.infrastructure.database.target import parse_target,schema_and_role
from reimbursement_service.integrations.security.terminal import Signer,Verifier,TrustedKey
from reimbursement_service.main import create_app
from reimbursement_service.modules.access.domain import VerifiedActor,request_hash
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest
from reimbursement_service.modules.operations.domain import Identity,Result,OperationFailure
from reimbursement_service.modules.operations.service import OperationsService
from verify_database import fingerprint

ROOT=Path(__file__).resolve().parents[2]


class ProofFailure(RuntimeError):pass


def actor(identity,action=None,expires=None):
    return VerifiedActor(identity.actor_id,identity.tenant_id,str(uuid4()),1,action or identity.kind,
        identity.operation_id,identity.request_hash,int(time.time()),expires or int(time.time())+60,False)


def identity(tenant,person):
    return Identity(tenant,str(uuid4()),person,'platform-dispatcher','claim.decide',str(uuid4()),'a'*64,'claim',str(uuid4()),1)


class Effect:
    def __init__(self,intent,schema,*,entered=None,release=None,fail=False):
        self.intent,self.schema,self.entered,self.release,self.fail=intent,schema,entered,release,fail
        self.calls=0
    async def apply(self,connection):
        self.calls+=1
        if self.entered:self.entered.set()
        if self.release:await self.release.wait()
        await connection.execute(sql.SQL('INSERT INTO {}.effects VALUES (%s,%s)').format(sql.Identifier(self.schema)),
                                 (self.intent.tenant_id,self.intent.operation_id))
        if self.fail:raise ValueError('deliberate feature fixture failure')
        return 'committed',Result(self.intent.object_id,1,'fixture_effect')


async def verify(admin):
    schema='ref01_probe_'+uuid4().hex;effects='ref03_effect_'+uuid4().hex
    runtime=replace(admin,user=schema_and_role(schema),password=secrets.token_urlsafe(48))
    db=None;binding=None;effect_oid=None;stage='preflight';checks=[]
    before=fingerprint();old_sql=sha256((ROOT/'reference_service/src/reimbursement_service/infrastructure/database/migrations/0001_foundation.sql').read_bytes()).hexdigest()
    private=Ed25519PrivateKey.generate();signer=Signer(kid='fixture',private_bytes=private.private_bytes_raw())
    verifier=Verifier({'fixture':TrustedKey(private.public_key().public_bytes_raw(),int(time.time())-60,int(time.time())+600)})
    tenant,person=str(uuid4()),str(uuid4())

    def require(condition,label=''):
        if not condition:raise ProofFailure(stage+(':'+label if label else ''))

    async def operator(statement,params=None):
        async with direct_connection(admin) as connection:
            cursor=await connection.execute(statement,params)
            return await cursor.fetchall() if cursor.description else []

    async def scalar(statement,params=None):return (await operator(statement,params))[0][0]

    async def count(op):return await scalar(sql.SQL('SELECT count(*) FROM {}.effects WHERE operation_id=%s').format(sql.Identifier(effects)),(op,))

    async def denied(awaitable,status):
        try:await awaitable
        except OperationFailure as failure:require(failure.status==status)
        else:raise ProofFailure(stage)

    try:
        require(not await operator('SELECT 1 FROM pg_namespace WHERE nspname IN (%s,%s)',(schema,effects)))
        require(not await operator('SELECT 1 FROM pg_roles WHERE rolname=%s',(runtime.user,)))
        stage='upgrade_0001_to_0002_preserves_runtime_and_tenant'
        require(await migrate(admin,runtime,schema=schema,upgrade=True,target_version=1)==(1,1))
        binding=(await operator('SELECT oid FROM pg_roles WHERE rolname=%s',(runtime.user,)))[0][0]
        await operator(sql.SQL('INSERT INTO {}.tenant_registry(tenant_id,provisioning_digest,active) VALUES(%s,%s,true)').format(sql.Identifier(schema)),(tenant,'c'*64))
        require(await migrate(admin,runtime,schema=schema,upgrade=True)==(2,2))
        require(await migrate(admin,runtime,schema=schema,upgrade=True)==(2,2))
        require(await scalar(sql.SQL('SELECT provisioning_digest FROM {}.tenant_registry WHERE tenant_id=%s').format(sql.Identifier(schema)),(tenant,))=='c'*64)
        checks.append(stage)
        # An external, owned fixture table models a future feature's atomic UoW
        # effect, not a claim or a production business table.
        await operator(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(effects)))
        effect_oid=await scalar('SELECT oid FROM pg_namespace WHERE nspname=%s',(effects,))
        async with direct_connection(admin) as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL('CREATE TABLE {}.effects(tenant_id uuid NOT NULL,operation_id uuid PRIMARY KEY)').format(sql.Identifier(effects)))
                await connection.execute(sql.SQL('ALTER TABLE {}.effects ENABLE ROW LEVEL SECURITY').format(sql.Identifier(effects)))
                await connection.execute(sql.SQL('ALTER TABLE {}.effects FORCE ROW LEVEL SECURITY').format(sql.Identifier(effects)))
                await connection.execute(sql.SQL('CREATE POLICY owner ON {}.effects TO CURRENT_USER USING(true) WITH CHECK(true)').format(sql.Identifier(effects)))
                await connection.execute(sql.SQL('CREATE POLICY scoped ON {}.effects TO {} USING(tenant_id={}.context_tenant()) WITH CHECK(tenant_id={}.context_tenant())').format(
                    sql.Identifier(effects),sql.Identifier(runtime.user),sql.Identifier(schema),sql.Identifier(schema)))
                await connection.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(effects),sql.Identifier(runtime.user)))
                await connection.execute(sql.SQL('GRANT SELECT,INSERT ON {}.effects TO {}').format(sql.Identifier(effects),sql.Identifier(runtime.user)))
        db=RuntimeDatabase(runtime,schema,max_size=2,limits=Limits(lock_ms=3000));await db.open();repo=PostgresOperations(db);service=OperationsService(repo,signer)
        stage='absent_lookup_is_unknown_not_no_commit'
        sample=identity(tenant,person)
        await denied(repo.lookup(sample.operation_id,actor(sample)),404);checks.append(stage)
        stage='commit_exact_replay_and_dual_identity_conflicts'
        effect=Effect(sample,effects);first=await repo.commit(sample,actor(sample),effect)
        require((await repo.commit(sample,actor(sample),effect))==first and effect.calls==1 and await count(sample.operation_id)==1)
        for changed in [replace(sample,request_hash='b'*64),replace(sample,key=str(uuid4())),replace(sample,operation_id=str(uuid4()))]:
            await denied(repo.commit(changed,actor(changed),Effect(changed,effects)),409)
        checks.append(stage)
        stage='rejected_is_terminal_replayable_and_not_a_release_proof'
        rejected_item=identity(tenant,person)
        class Reject:
            calls=0
            async def apply(self,connection):
                self.calls+=1
                return 'rejected',Result(None,None,'fixture_rejected')
        rejected_work=Reject()
        rejection=await repo.commit(rejected_item,actor(rejected_item),rejected_work)
        require(rejection.state=='rejected' and await count(rejected_item.operation_id)==0)
        require(await repo.commit(rejected_item,actor(rejected_item),rejected_work)==rejection and rejected_work.calls==1)
        require(await repo.seal(rejected_item,actor(rejected_item))==rejection)
        try:verifier.verify(signer.issue(rejection,authority_deadline=int(time.time())+60),rejected_item)
        except OperationFailure:pass
        else:raise ProofFailure(stage)
        checks.append(stage)
        stage='simultaneous_absent_same_operation_only_one_effect'
        item=identity(tenant,person);work=Effect(item,effects)
        outcomes=await asyncio.gather(repo.commit(item,actor(item),work),repo.commit(item,actor(item),work))
        require(outcomes[0]==outcomes[1] and work.calls==1 and await count(item.operation_id)==1);checks.append(stage)
        stage='same_key_new_operation_race_only_one_wins'
        left=identity(tenant,person);right=replace(left,operation_id=str(uuid4()))
        outcomes=await asyncio.gather(repo.commit(left,actor(left),Effect(left,effects)),repo.commit(right,actor(right),Effect(right,effects)),return_exceptions=True)
        require(sum(isinstance(value,OperationFailure) and value.status==409 for value in outcomes)==1)
        require(await count(left.operation_id)+await count(right.operation_id)==1);checks.append(stage)
        stage='seal_wins_before_delayed_request_and_forbids_effect'
        item=identity(tenant,person);work=Effect(item,effects)
        sealed=await repo.seal(item,actor(item))
        late=await repo.commit(item,actor(item),work)
        require(sealed==late and late.state=='sealed_no_commit' and work.calls==0 and await count(item.operation_id)==0);checks.append(stage)
        stage='inflight_commit_holds_seal_then_returns_committed'
        item=identity(tenant,person);entered=asyncio.Event();release=asyncio.Event();work=Effect(item,effects,entered=entered,release=release)
        committing=asyncio.create_task(repo.commit(item,actor(item),work))
        async with asyncio.timeout(10):await entered.wait()
        sealing=asyncio.create_task(repo.seal(item,actor(item)))
        await asyncio.sleep(.05);require(not sealing.done());release.set()
        done,seal_result=await asyncio.gather(committing,sealing)
        require(done==seal_result and done.state=='committed' and await count(item.operation_id)==1);checks.append(stage)
        stage='simultaneous_absent_commit_and_seal_one_terminal'
        item=identity(tenant,person);work=Effect(item,effects)
        done,sealed=await asyncio.gather(repo.commit(item,actor(item),work),repo.seal(item,actor(item)))
        require(done==sealed and await count(item.operation_id)==(1 if done.state=='committed' else 0));checks.append(stage)
        stage='callback_failure_rolls_back_effect_ledger_audit'
        item=identity(tenant,person)
        try:await repo.commit(item,actor(item),Effect(item,effects,fail=True))
        except (ValueError,OperationFailure):pass
        else:raise ProofFailure(stage)
        require(await count(item.operation_id)==0)
        await denied(repo.lookup(item.operation_id,actor(item)),404)
        require(await scalar(sql.SQL('SELECT count(*) FROM {}.service_audit WHERE operation_id=%s').format(sql.Identifier(schema)),(item.operation_id,))==0);checks.append(stage)
        stage='authority_expires_while_waiting_before_effect'
        item=identity(tenant,person);entered=asyncio.Event();release=asyncio.Event()
        work=Effect(item,effects,entered=entered,release=release)
        running=asyncio.create_task(repo.commit(item,actor(item),work))
        async with asyncio.timeout(10):await entered.wait()
        waiting=asyncio.create_task(repo.seal(item,actor(item,expires=int(time.time())+1)))
        await asyncio.sleep(1.1);release.set();await running
        await denied(waiting,401);require(await count(item.operation_id)==1);checks.append(stage)
        stage='lost_response_reconnect_lookup_and_replay_no_second_effect'
        item=identity(tenant,person);work=Effect(item,effects);committed=await repo.commit(item,actor(item),work)
        # Discard first response and close the pool, then reconnect with the same
        # service credential/identity; this is not a new business operation.
        await db.close();db=RuntimeDatabase(runtime,schema,max_size=2);await db.open();repo=PostgresOperations(db);service=OperationsService(repo,signer)
        require(await repo.lookup(item.operation_id,actor(item))==committed)
        require(await repo.commit(item,actor(item),work)==committed and work.calls==1);checks.append(stage)
        stage='signing_failure_after_commit_then_authorized_lookup_resigns'
        terminal=committed
        try:service.proofs=Signer();service.response(terminal,actor(item))
        except OperationFailure as failure:require(failure.status==503)
        else:raise ProofFailure(stage)
        service.proofs=signer
        response=service.response(await repo.lookup(item.operation_id,actor(item)),actor(item))
        require(verifier.verify(response['proof'],item)==terminal and await count(item.operation_id)==1);checks.append(stage)
        stage='owner_tenant_expiry_and_private_tables_denied'
        await denied(repo.lookup(item.operation_id,replace(actor(item),actor_id=str(uuid4()))),404)
        await denied(repo.lookup(item.operation_id,replace(actor(item),tenant_id=str(uuid4()))),404)
        await denied(repo.lookup(item.operation_id,replace(actor(item),expires_at=1)),401)
        await operator(sql.SQL('UPDATE {}.tenant_registry SET active=false WHERE tenant_id=%s').format(sql.Identifier(schema)),(tenant,))
        await denied(repo.lookup(item.operation_id,actor(item)),404)
        await operator(sql.SQL('UPDATE {}.tenant_registry SET active=true WHERE tenant_id=%s').format(sql.Identifier(schema)),(tenant,))
        from reimbursement_service.modules.system.domain import DatabaseUnavailable
        for table in ('service_operations','service_audit'):
            try:
                async with db.transaction() as connection:await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table)))
            except DatabaseUnavailable:pass
            else:raise ProofFailure(stage)
        checks.append(stage)
        stage='privilege_drift_fails_closed_before_ledger_work'
        await operator(sql.SQL('GRANT SELECT ON {}.service_operations TO {}').format(sql.Identifier(schema),sql.Identifier(runtime.user)))
        try:require(not await db.ping())
        finally:await operator(sql.SQL('REVOKE SELECT ON {}.service_operations FROM {}').format(sql.Identifier(schema),sql.Identifier(runtime.user)))
        require(await db.ping());checks.append(stage)
        stage='actual_api_owner_lookup_and_recovery_credential_scope'
        signing_diagnostic=[]
        class DiagnosticProofs:
            def issue(self,terminal,*,authority_deadline):
                try:value=signer.issue(terminal,authority_deadline=authority_deadline)
                except OperationFailure as failure:
                    trace=failure.__traceback__;line=0
                    while trace is not None:
                        if trace.tb_frame.f_code.co_filename.endswith('terminal.py'):line=trace.tb_lineno
                        trace=trace.tb_next
                    signing_diagnostic.append('sign-failed-completed-delta-'+str(terminal.completed_at-int(time.time()))+'-remaining-'+str(authority_deadline-int(time.time())))
                    signing_diagnostic.append('signline-'+str(line))
                    raise
                signing_diagnostic.append('sign-pass')
                return value
        service.proofs=DiagnosticProofs()
        lookup_payload=dict(schema_version=1,action='operation.read',tenant_id=tenant,actor_id=person,operation_id=str(uuid4()),
            object=dict(kind='operation',id=item.operation_id,version=1),data={})
        class ControlledVerifier:
            async def verify(self,delegation,payload):
                return VerifiedActor(payload['actor_id'],payload['tenant_id'],str(uuid4()),1,payload['action'],payload['operation_id'],request_hash(payload),int(time.time()),int(time.time())+60,False)
        dispatcher,recovery_secret=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        def access(secret):return AccessService(ControlledVerifier(),Dispatcher('platform-dispatcher',credential_digest(secret),frozenset({'operation.read'})))
        # Actual actor verification was accepted REF-02; this fixture isolates
        # route/ledger ownership/recovery integration without replaying that suite.
        from reimbursement_service.modules.system.service import HealthService
        app=create_app(Settings(database_url='unused fixture'),resource_factory=lambda settings:(db,HealthService(db)),
                       operations=service,dispatch=access(dispatcher),recovery=access(recovery_secret))
        headers={'Authorization':'Bearer '+dispatcher,'X-EAI-Delegation':'controlled.fixture','Content-Type':'application/json',
                 'X-EAI-Operation-Payload':base64.urlsafe_b64encode(json.dumps(lookup_payload).encode()).decode().rstrip('=')}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://service.test') as client:
            response=await client.get('/service/v1/operations/'+item.operation_id,headers=headers)
            require(response.status_code==200,'lookup-'+str(response.status_code))
            new=identity(tenant,person);lookup_payload['object']['id']=new.operation_id
            body=dict(payload=lookup_payload,identity=new.wire())
            path='/service/v1/operations/'+new.operation_id+'/seal-if-absent'
            response=await client.post(path,headers=headers,json=body)
            require(response.status_code==401,'ordinary-credential-'+str(response.status_code))
            response=await client.post(path,headers={**headers,'Authorization':'Bearer '+recovery_secret},json=body)
            if response.status_code!=200:
                try:
                    saved=await repo.lookup(new.operation_id,actor(new))
                    diagnostic='terminal-'+saved.state+'-clockdelta-'+str(saved.completed_at-int(time.time()))
                except OperationFailure as failure:diagnostic='lookup-'+str(failure.status)
                raise ProofFailure('recovery-'+str(response.status_code)+':'+diagnostic+':'+','.join(signing_diagnostic))
            require(response.json()['operation']['state']=='sealed_no_commit','recovery-state')
            require(verifier.verify(response.json()['proof'],new).state=='sealed_no_commit')
        checks.append(stage)
        stage='terminal_and_audit_one_to_one'
        require(await scalar(sql.SQL('SELECT count(*) FROM {}.service_operations').format(sql.Identifier(schema)))==
                await scalar(sql.SQL('SELECT count(*) FROM {}.service_audit').format(sql.Identifier(schema))));checks.append(stage)
    except Exception as exc:
        detail=str(exc) if type(exc) is ProofFailure else type(exc).__name__
        current=exc
        while current is not None:
            state=getattr(current,'sqlstate',None)
            if isinstance(state,str) and len(state)==5 and state.isalnum():
                position=getattr(getattr(current,'diag',None),'statement_position',None)
                detail+=':sqlstate='+state+':position='+str(position)
                break
            current=current.__context__
        raise ProofFailure(stage+':'+detail) from None
    finally:
        if db is not None:await db.close()
        async with direct_connection(admin) as connection:
            if effect_oid is not None:
                actual=await (await connection.execute('SELECT oid FROM pg_namespace WHERE nspname=%s',(effects,))).fetchone()
                if actual!=(effect_oid,):raise ProofFailure('effect-cleanup-binding')
                await connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(effects)))
            row=await (await connection.execute('SELECT oid FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()
            role=await (await connection.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone()
            if row is not None:
                owned=await (await connection.execute(sql.SQL('SELECT role_name,role_oid FROM {}.runtime_binding').format(sql.Identifier(schema)))).fetchall()
                if owned!=[(runtime.user,role[0] if role else None)] or (binding is not None and role!=(binding,)):raise ProofFailure('cleanup-binding')
                await connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                dbname=(await (await connection.execute('SELECT current_database()')).fetchone())[0]
                await connection.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(sql.Identifier(dbname),sql.Identifier(runtime.user)))
                await connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(runtime.user)))
            elif role is not None:raise ProofFailure('cleanup-unbound-role')
    if before!=fingerprint():raise ProofFailure('source-drift')
    if old_sql!=sha256((ROOT/'reference_service/src/reimbursement_service/infrastructure/database/migrations/0001_foundation.sql').read_bytes()).hexdigest():raise ProofFailure('immutable-0001')
    return dict(status='PASS',checks=checks,cleanup=True,source_hashes=before,immutable_0001_sha256=old_sql,
        fixture='actual service PostgreSQL with isolated effect table, controlled actor-verifier port for route tests; REF02 actual actor proof reused',
        actual_claims=False,cloud_deployment=False)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    try:
        env=dotenv_values(args.env_file);admin=parse_target(env.get('DATABASE_DIRECT_URL',''),direct=True)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(admin))
    except Exception as exc:
        label=str(exc) if type(exc) is ProofFailure else type(exc).__name__
        args.output.write_text(json.dumps(dict(status='FAIL',case=label),indent=2)+'\n',encoding='utf-8')
        print('REF03 verification FAIL: '+label);raise SystemExit(1) from None
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(f"REF03 verification PASS: {len(result['checks'])} groups; owned cleanup complete.")


if __name__=='__main__':main()
