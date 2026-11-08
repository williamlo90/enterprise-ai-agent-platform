"""REF-04 owned real service DB/API proof; gated authority fixtures are explicit."""
import argparse
import asyncio
import base64
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import secrets
import time
from uuid import UUID,uuid4
import httpx
from psycopg import sql
from dotenv import dotenv_values
from reimbursement_service.composition import database_loop_factory
from reimbursement_service.config import Settings
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase,direct_connection,Limits
from reimbursement_service.infrastructure.database.migration import migrate,revisions
from reimbursement_service.infrastructure.database.reference import PostgresTrips,PostgresPackages
from reimbursement_service.infrastructure.database.operations import PostgresOperations
from reimbursement_service.infrastructure.database.target import parse_target,schema_and_role
from reimbursement_service.main import create_app
from reimbursement_service.modules.access.domain import CatalogAuthority,CatalogBinding,VerifiedActor,AccessFailure,request_hash
from reimbursement_service.modules.access.service import AccessService,Dispatcher,DefaultDenyScope,credential_digest
from reimbursement_service.modules.policies.domain import Package,canonical_body,encoded,request_digest
from reimbursement_service.modules.policies.service import PackagesService
from reimbursement_service.modules.trips.domain import Trip,ExpenseItem,listing_digest
from reimbursement_service.modules.trips.service import TripsService
from reimbursement_service.modules.system.domain import ActorContext,DatabaseUnavailable
from reimbursement_service.modules.system.service import HealthService
from reimbursement_service.modules.operations.domain import Identity
from seed_reference_fixture import seed_trips
from verify_database import fingerprint

ROOT=Path(__file__).resolve().parents[2]


class Failure(RuntimeError):pass


def package_body(tenant,pid,version=1,days=30):
    value=dict(schema_version=1,digest='0'*64,valid_trip_end_from='2026-01-01',valid_trip_end_to='2027-01-01',timezone='Asia/Jakarta',
        document_versions=[dict(document_id=str(uuid4()),version_id=str(uuid4()),content_digest='a'*64)],
        rules=dict(currency='IDR',submission_deadline_days=days,max_total_minor=None,
            categories=[dict(code='transport',receipt_required=True,max_amount_minor=1000000)],
            approval_route=[dict(stage=1,approver_id=str(uuid4()),scope_ref='reimbursement')]))
    return rebind(value,tenant,pid,version)


def rebind(value,tenant,pid,version):
    body=json.loads(canonical_body(value));body.pop('digest')
    body['digest']=sha256(b'reimbursement:rule-package:v1:'+encoded(dict(tenant_id=tenant,package_id=pid,package_version=version,**body))).hexdigest()
    return body


class ControlledScopes:
    """Test-only authority port, NOT an AUTH-04 list/publisher implementation."""
    def __init__(self,tenant,person):self.tenant,self.person,self.mode=tenant,person,'ok';self.calls=0
    async def authorize(self,headers,binding):
        self.calls+=1
        if self.mode=='denied':
            from reimbursement_service.modules.access.domain import FailureKind
            raise AccessFailure(FailureKind.FORBIDDEN)
        if self.mode=='down':
            from reimbursement_service.modules.access.domain import FailureKind
            raise AccessFailure(FailureKind.UNAVAILABLE)
        if self.mode=='wrong':binding=replace(binding,request_digest='f'*64)
        now=int(time.time());expired=self.mode=='expired'
        return CatalogAuthority(binding,self.tenant,self.person,'platform-dispatcher',now-61 if expired else now,now-1 if expired else now+60)


def trip_actor(tenant,person,trip_id):
    payload=dict(schema_version=1,action='trip.read',tenant_id=tenant,actor_id=person,operation_id=str(uuid4()),
                 object=dict(kind='trip',id=trip_id,version=1),data={})
    now=int(time.time())
    return VerifiedActor(person,tenant,str(uuid4()),1,'trip.read',payload['operation_id'],request_hash(payload),now,now+60,False),payload


async def verify(admin):
    schema='ref01_probe_'+uuid4().hex;runtime=replace(admin,user=schema_and_role(schema),password=secrets.token_urlsafe(48))
    db=None;role_oid=None;schema_oid=None;stage='preflight';checks=[];before=fingerprint()
    immutable={p.name:sha256(p.read_bytes()).hexdigest() for p in (ROOT/'reference_service/src/reimbursement_service/infrastructure/database/migrations').glob('000[12]_*.sql')}
    tenant,other,owner,colleague=str(uuid4()),str(uuid4()),str(uuid4()),str(uuid4())

    def require(value,label=''):
        if not value:raise Failure(stage+(':'+label if label else ''))

    async def operator(statement,params=None):
        async with direct_connection(admin) as connection:
            cursor=await connection.execute(statement,params)
            return await cursor.fetchall() if cursor.description else []

    async def scalar(statement,params=None):return (await operator(statement,params))[0][0]

    async def status(awaitable,expected):
        try:await awaitable
        except AccessFailure as error:require(error.status in (expected if isinstance(expected,tuple) else (expected,)),'status-'+str(error.status))
        else:raise Failure(stage+':unexpected-allow')

    def publish_authority(body,pid,version,key=None,operation=None,person=owner,scope_tenant=tenant):
        binding=CatalogBinding('policy.publish',pid,version,request_digest(pid,version,canonical_body(body)),operation or str(uuid4()),key or str(uuid4()))
        now=int(time.time());return CatalogAuthority(binding,scope_tenant,person,'platform-dispatcher',now,now+60)

    try:
        require(not await operator('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,)))
        require(not await operator('SELECT 1 FROM pg_roles WHERE rolname=%s',(runtime.user,)))
        stage='upgrade_0002_to_0003_preserves_existing_registry_and_ledger'
        require(await migrate(admin,runtime,schema=schema,upgrade=True,target_version=2)==(2,2))
        role_oid=await scalar('SELECT oid FROM pg_roles WHERE rolname=%s',(runtime.user,))
        schema_oid=await scalar('SELECT oid FROM pg_namespace WHERE nspname=%s',(schema,))
        for tid in (tenant,other):await operator(sql.SQL('INSERT INTO {}.tenant_registry(tenant_id,provisioning_digest,active) VALUES(%s,%s,true)').format(sql.Identifier(schema)),(tid,'b'*64))
        db=RuntimeDatabase(runtime,schema,max_size=2,limits=Limits(lock_ms=5000,statement_ms=6000));await db.open()
        legacy=Identity(tenant,str(uuid4()),owner,'platform-dispatcher','claim.decide',str(uuid4()),'a'*64,'claim',str(uuid4()),1)
        legacy_actor=VerifiedActor(owner,tenant,str(uuid4()),1,'operation.read',str(uuid4()),'a'*64,int(time.time()),int(time.time())+60,False)
        sealed=await PostgresOperations(db).seal(legacy,legacy_actor)
        await db.close();db=None
        require(await migrate(admin,runtime,schema=schema,upgrade=True)==(3,3))
        require(await migrate(admin,runtime,schema=schema,upgrade=True)==(3,3))
        db=RuntimeDatabase(runtime,schema,max_size=2,limits=Limits(lock_ms=3000));await db.open()
        require(await PostgresOperations(db).lookup(legacy.operation_id,replace(legacy_actor,expires_at=int(time.time())+60))==sealed)
        checks.append(stage)
        trip_ids=sorted(str(uuid4()) for _ in range(3));foreign=str(uuid4());colleague_trip=str(uuid4())
        def make_trip(identifier):return Trip(identifier,'2026-11-01','2026-11-03','completed','fictional-employee',
            (ExpenseItem(str(uuid4()),'2026-10-31','transport',100000,'IDR'),ExpenseItem(str(uuid4()),'2026-11-02','hotel',200000,'IDR')))
        own_trips=tuple(make_trip(tid) for tid in trip_ids)
        stage='operator_fixture_seed_replay_preserves_owned_source'
        async with direct_connection(admin) as connection:
            require(await seed_trips(connection,schema,tenant,owner,own_trips)==3)
            require(await seed_trips(connection,schema,tenant,owner,own_trips)==3)
            await seed_trips(connection,schema,tenant,colleague,(make_trip(colleague_trip),))
            await seed_trips(connection,schema,other,owner,(make_trip(foreign),))
        checks.append(stage)
        trips_repo=PostgresTrips(db);packages_repo=PostgresPackages(db);scopes=ControlledScopes(tenant,owner)
        trips=TripsService(trips_repo,scopes);packages=PackagesService(packages_repo,scopes)
        stage='single_trip_exact_owner_tenant_and_integer_declared_items'
        person,payload=trip_actor(tenant,owner,trip_ids[0])
        detail,_=await trips.detail(trip_ids[0],person,payload)
        require(detail==own_trips[0].wire())
        for tid in (foreign,colleague_trip,str(uuid4())):
            person,payload=trip_actor(tenant,owner,tid);await status(trips.detail(tid,person,payload),404)
        checks.append(stage)
        stage='same_tenant_owner_cursor_pages_no_totals_or_item_leak'
        first,_=await trips.listing([],str(uuid4()),limit=2)
        require([v['id'] for v in first['items']]==trip_ids[:2] and first['next_cursor']==trip_ids[1])
        require(set(first)=={'items','next_cursor'} and all('items' not in v for v in first['items']))
        last,_=await trips.listing([],str(uuid4()),after=first['next_cursor'],limit=2)
        require([v['id'] for v in last['items']]==trip_ids[2:] and last['next_cursor'] is None)
        for cursor in (foreign,colleague_trip,str(uuid4())):await status(trips.listing([],str(uuid4()),after=cursor),404)
        checks.append(stage)
        stage='composite_item_fk_rejects_foreign_tenant_or_owner'
        for tid,person in [(other,owner),(tenant,colleague)]:
            try:
                await operator(sql.SQL('INSERT INTO {}.trip_expense_items VALUES(%s,%s,%s,%s,%s,%s,%s,%s)').format(sql.Identifier(schema)),
                    (tid,str(uuid4()),trip_ids[0],person,'2026-11-01','transport',1,'IDR'))
            except DatabaseUnavailable:pass
            else:raise Failure(stage)
        checks.append(stage)
        stage='received_copy_and_receipt_stable_replay_after_reauthorization'
        pid=str(uuid4());body=package_body(tenant,pid);key=str(uuid4());operation=str(uuid4())
        result,_=await packages.receive([],pid,1,key,operation,body)
        replay,_=await packages.receive([],pid,1,key,operation,body)
        require(result==replay and result['state']=='received' and result['digest']==body['digest'])
        copied=await scalar(sql.SQL('SELECT canonical FROM {}.service_policy_packages WHERE tenant_id=%s AND package_id=%s').format(sql.Identifier(schema)),(tenant,pid))
        require(json.loads(copied)==Package(tenant,pid,1,canonical_body(body)).content)
        checks.append(stage)
        stage='version_digest_key_operation_alias_and_foreign_digest_conflicts'
        changed=json.loads(json.dumps(body));changed['rules']['submission_deadline_days']=14;changed=rebind(changed,tenant,pid,1)
        await status(packages.receive([],pid,1,str(uuid4()),str(uuid4()),changed),409)
        await status(packages.receive([],pid,1,key,operation,changed),409)
        await status(packages.receive([],pid,1,key,str(uuid4()),body),409)
        await status(packages.receive([],pid,1,str(uuid4()),operation,body),409)
        bad={**body,'digest':'f'*64};await status(packages.receive([],pid,1,str(uuid4()),str(uuid4()),bad),409)
        alien=package_body(other,pid);await status(packages.receive([],pid,1,str(uuid4()),str(uuid4()),alien),409)
        require(await scalar(sql.SQL('SELECT count(*) FROM {}.service_policy_packages WHERE tenant_id=%s AND package_id=%s').format(sql.Identifier(schema)),(tenant,pid))==1)
        checks.append(stage)
        stage='same_copy_new_key_and_overlapping_received_version_never_activates'
        same,_=await packages.receive([],pid,1,str(uuid4()),str(uuid4()),body)
        require(same==result)
        next_body=rebind(body,tenant,pid,2)
        second,_=await packages.receive([],pid,2,str(uuid4()),str(uuid4()),next_body)
        require(second['state']=='received' and second['package_version']==2)
        checks.append(stage)
        stage='concurrent_same_version_different_digest_one_winner'
        race_pid=str(uuid4());left=package_body(tenant,race_pid);right=json.loads(json.dumps(left));right['rules']['submission_deadline_days']=14;right=rebind(right,tenant,race_pid,1)
        answers=await asyncio.gather(packages.receive([],race_pid,1,str(uuid4()),str(uuid4()),left),
            packages.receive([],race_pid,1,str(uuid4()),str(uuid4()),right),return_exceptions=True)
        require(sum(isinstance(a,AccessFailure) and a.status==409 for a in answers)==1)
        require(await scalar(sql.SQL('SELECT count(*) FROM {}.service_policy_packages WHERE package_id=%s').format(sql.Identifier(schema)),(race_pid,))==1)
        checks.append(stage)
        stage='scope_unbound_denied_outage_expired_and_misbound_hold_no_write'
        hold_pid=str(uuid4());hold=package_body(tenant,hold_pid)
        for mode,expected in [('denied',403),('down',503),('expired',503),('wrong',403)]:
            scopes.mode=mode
            await status(packages.receive([],hold_pid,1,str(uuid4()),str(uuid4()),hold),expected)
            await status(trips.listing([],str(uuid4())),expected)
        scopes.mode='ok'
        await status(PackagesService(packages_repo,DefaultDenyScope()).receive([],hold_pid,1,str(uuid4()),str(uuid4()),hold),503)
        require(await scalar(sql.SQL('SELECT count(*) FROM {}.service_policy_packages WHERE package_id=%s').format(sql.Identifier(schema)),(hold_pid,))==0)
        checks.append(stage)
        stage='publisher_expiry_while_key_lock_wait_rolls_back_copy_and_receipt'
        exp_pid=str(uuid4());exp_body=package_body(tenant,exp_pid);grant=publish_authority(exp_body,exp_pid,1)
        package=Package(tenant,exp_pid,1,canonical_body(exp_body))
        async with direct_connection(admin) as blocking:
            async with blocking.transaction():
                await blocking.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                    ('reimbursement:package-key:'+tenant+':'+owner+':'+grant.binding.key,))
                database_now=(await (await blocking.execute('SELECT floor(extract(epoch FROM clock_timestamp()))::bigint')).fetchone())[0]
                moment=max(int(time.time()),database_now);grant=replace(grant,verified_at=moment-58,expires_at=moment+2)
                pending=asyncio.create_task(packages_repo.receive(package,grant.binding.key,grant))
                await asyncio.sleep(.05);require(not pending.done(),'request-is-waiting')
                await asyncio.sleep(2.1)
            # DB and local clocks may differ; either authoritative SQL expiry
            # (401) or final local transaction expiry (503) must roll back both.
            await status(pending,(401,503))
        require(await scalar(sql.SQL('SELECT count(*) FROM {}.service_policy_packages WHERE package_id=%s').format(sql.Identifier(schema)),(exp_pid,))==0)
        require(await scalar(sql.SQL('SELECT count(*) FROM {}.package_receipts WHERE key=%s').format(sql.Identifier(schema)),(grant.binding.key,))==0)
        checks.append(stage)
        stage='private_tables_direct_runtime_denial_and_privilege_drift'
        for table in ('trips','trip_expense_items','service_policy_packages','package_receipts'):
            try:
                async with db.transaction(ActorContext(UUID(tenant),UUID(owner))) as connection:
                    await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table)))
            except DatabaseUnavailable:pass
            else:raise Failure(stage)
        await operator(sql.SQL('GRANT SELECT ON {}.trips TO {}').format(sql.Identifier(schema),sql.Identifier(runtime.user)))
        try:require(not await db.ping())
        finally:await operator(sql.SQL('REVOKE SELECT ON {}.trips FROM {}').format(sql.Identifier(schema),sql.Identifier(runtime.user)))
        require(await db.ping());checks.append(stage)
        stage='actual_optional_api_private_reads_publisher_and_default_gate'
        secret=secrets.token_urlsafe(32)
        class ControlledActor:
            async def verify(self,delegation,payload):
                now=int(time.time());return VerifiedActor(payload['actor_id'],payload['tenant_id'],str(uuid4()),1,payload['action'],payload['operation_id'],request_hash(payload),now,now+60,False)
        access=AccessService(ControlledActor(),Dispatcher('platform-dispatcher',credential_digest(secret),frozenset({'trip.read'})))
        app=create_app(Settings(database_url='fixture'),resource_factory=lambda settings:(db,HealthService(db)),trips=trips,packages=packages,trip_dispatch=access)
        headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json','X-EAI-Delegation':'controlled.fixture','X-EAI-Operation-ID':str(uuid4())}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://service.test') as client:
            require((await client.get('/service/v1/trips?limit=2',headers=headers)).status_code==200)
            require((await client.get('/service/v1/trips?limit=2&limit=1',headers=headers)).status_code==400)
            person,payload=trip_actor(tenant,owner,trip_ids[0])
            detail_headers={**headers,'X-EAI-Trip-Payload':base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')}
            require((await client.get('/service/v1/trips/'+trip_ids[0],headers=detail_headers)).status_code==200)
            new_pid=str(uuid4());new_body=package_body(tenant,new_pid)
            path='/service/v1/policy-packages/'+new_pid+'/versions/1'
            receipt=await client.put(path,headers={**headers,'Idempotency-Key':str(uuid4())},json=new_body)
            require(receipt.status_code==200 and receipt.json()['state']=='received')
            require((await client.post('/service/v1/trips',headers=headers,json={})).status_code==405)
            require((await client.put(path,headers={**headers,'Idempotency-Key':str(uuid4())},json={**new_body,'owner_id':owner})).status_code==400)
        gated_app=create_app(Settings(database_url='fixture'),resource_factory=lambda settings:(db,HealthService(db)),
            trips=TripsService(trips_repo,DefaultDenyScope()),packages=PackagesService(packages_repo,DefaultDenyScope()),trip_dispatch=access)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(gated_app),base_url='https://service.test') as client:
            require((await client.get('/service/v1/trips',headers=headers)).status_code==503)
            require((await client.put(path,headers={**headers,'Idempotency-Key':str(uuid4())},json=new_body)).status_code==503)
        checks.append(stage)
    except Exception as exc:
        detail=str(exc) if type(exc) is Failure else type(exc).__name__;current=exc
        while current is not None:
            state=getattr(current,'sqlstate',None)
            if isinstance(state,str) and len(state)==5 and state.isalnum():
                detail+=':sqlstate='+state+':position='+str(getattr(getattr(current,'diag',None),'statement_position',None));break
            current=current.__context__
        raise Failure(stage+':'+detail) from None
    finally:
        if db is not None:await db.close()
        async with direct_connection(admin) as connection:
            namespace=await (await connection.execute('SELECT oid FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()
            role=await (await connection.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone()
            if namespace is not None:
                binding=await (await connection.execute(sql.SQL('SELECT role_name,role_oid FROM {}.runtime_binding').format(sql.Identifier(schema)))).fetchall()
                if binding!=[(runtime.user,role[0] if role else None)] or (schema_oid is not None and namespace!=(schema_oid,)) or (role_oid is not None and role!=(role_oid,)):raise Failure('cleanup-binding')
                await connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                dbname=(await (await connection.execute('SELECT current_database()')).fetchone())[0]
                await connection.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(sql.Identifier(dbname),sql.Identifier(runtime.user)))
                await connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(runtime.user)))
            elif role is not None:raise Failure('cleanup-unbound-role')
    if before!=fingerprint():raise Failure('source-drift')
    require(all(sha256((ROOT/'reference_service/src/reimbursement_service/infrastructure/database/migrations'/name).read_bytes()).hexdigest()==value for name,value in immutable.items()),'immutable-prefix')
    return dict(status='PASS',checks=checks,cleanup=True,source_hashes=before,immutable_sql=immutable,
        actual_data='real isolated service PostgreSQL and ASGI API; fictional operator trips/items; controlled authority ports',
        actual_publisher_or_list_producer=False,actual_eligibility=False,cloud_deployment=False)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    try:
        target=parse_target(dotenv_values(args.env_file).get('DATABASE_DIRECT_URL',''),direct=True)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(target))
    except Exception as exc:
        label=str(exc) if type(exc) is Failure else type(exc).__name__
        args.output.write_text(json.dumps(dict(status='FAIL',case=label),indent=2)+'\n',encoding='utf-8');print('REF04 verification FAIL: '+label);raise SystemExit(1) from None
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(f"REF04 verification PASS: {len(result['checks'])} groups; owned cleanup complete.")


if __name__=='__main__':main()
