"""POL-01 actual loopback TLS publication across two isolated DB schemas/logins."""
import argparse
import asyncio
from contextlib import asynccontextmanager,AsyncExitStack
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
import secrets
import socket
import ssl
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4
import httpx
import uvicorn
from psycopg import sql
from psycopg.types.json import Jsonb
from check_policy_draft import Harness as KernelHarness,DRAFTS
from check_knowledge_workspace import verify
from check_reference_actor import certificate
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,Settings,load_settings
from enterprise_ai_platform.delegation_check import SigningFixture,ISSUER,AUDIENCE,CALLER
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database,finish_cleanup
from enterprise_ai_platform.infrastructure.database.delegation import PostgresDelegationRepository
from enterprise_ai_platform.infrastructure.database.policies import PostgresPolicyRepository
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyReceiver,PolicyTarget
from enterprise_ai_platform.integrations.reimbursement.trips import TripCatalogReader
from enterprise_ai_platform.integrations.security.catalog import RSACatalogSigner
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.modules.delegation.service import DelegationService
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.policies.service import PolicyService
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.trips.service import TripsService as PlatformTripsService
from enterprise_ai_platform.infrastructure.database.trips import PostgresTripRepository
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'reference_service/src'))
from reimbursement_service.config import Settings as ReferenceSettings
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase as ReferenceDatabase,direct_connection
from reimbursement_service.infrastructure.database.migration import migrate as migrate_reference
from reimbursement_service.infrastructure.database.target import Target,schema_and_role
from reimbursement_service.infrastructure.database.reference import PostgresPackages,PostgresTrips
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.integrations.platform.catalog import CatalogAuthorityAdapter
from reimbursement_service.main import create_app as create_reference_app
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest
from reimbursement_service.modules.policies.service import PackagesService
from reimbursement_service.modules.trips.service import TripsService
from reimbursement_service.modules.system.service import HealthService


@asynccontextmanager
async def tls_server(app,cert,key):
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(16);listener.setblocking(False)
    origin='https://127.0.0.1:'+str(listener.getsockname()[1])
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=listener.getsockname()[1],ssl_certfile=str(cert),ssl_keyfile=str(key),
        access_log=False,log_level='critical',proxy_headers=False,lifespan='off'))
    task=asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                require(not task.done(),'publication-tls-start');await asyncio.sleep(.02)
        yield origin
    finally:
        async def stop():
            server.should_exit=True
            try:
                try:
                    async with asyncio.timeout(5):await task
                finally:
                    if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            finally:listener.close()
        await finish_cleanup(stop())


class AckDelivery:
    """Transparent test-only hold AFTER real receiver commit, before ACK delivery."""
    def __init__(self,actual):self.actual,self.after_success=actual,None
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or self.after_success is None:return await self.actual(scope,receive,send)
        messages=[]
        async def capture(message):messages.append(message)
        await self.actual(scope,receive,capture)
        callback=self.after_success;self.after_success=None
        if messages and messages[0].get('status')==200:await callback()
        for message in messages:await send(message)


class Harness(KernelHarness):
    reference_admin=None

    def internal_application(self,delegation):
        return create_internal_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=None,delegation=delegation))

    def reference_application(self,database,platform_client,dispatcher,trust):
        scopes=CatalogAuthorityAdapter(platform_client,credential_digest(dispatcher))
        packages=PackagesService(PostgresPackages(database),scopes)
        trips=TripsService(PostgresTrips(database),scopes)
        access=AccessService(platform_client,Dispatcher('platform-dispatcher',credential_digest(dispatcher),frozenset({'trip.read'})))
        return create_reference_app(ReferenceSettings(database_url='fixture'),resource_factory=lambda settings:(database,HealthService(database)),trips=trips,packages=packages,trip_dispatch=access)

    def public_application(self,receiver,trip_reader,trust):
        self.policies=PolicyService(PostgresPolicyRepository(self.database),ORIGIN,receiver=receiver)
        self.trips=PlatformTripsService(PostgresTripRepository(self.database),trip_reader)
        settings=Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True})
        return create_app(settings,services=ApplicationServices(health=SimpleNamespace(),policies=self.policies,trips=self.trips))

    async def cleanup_reference(self,schema,runtime,role_oid,schema_oid):
        async with direct_connection(self.reference_admin) as connection:
            namespace=await (await connection.execute('SELECT oid FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()
            role=await (await connection.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone()
            if namespace is None:
                require(role is None,'publication-no-unbound-reference-role');return
            binding=await (await connection.execute(sql.SQL('SELECT role_name,role_oid FROM {}.runtime_binding').format(sql.Identifier(schema)))).fetchall()
            require((schema_oid is None or namespace==(schema_oid,)) and role is not None
                and (role_oid is None or role==(role_oid,)) and binding==[(runtime.user,role[0])],'publication-cleanup-reference-binding')
            await connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            dbname=(await (await connection.execute('SELECT current_database()')).fetchone())[0]
            await connection.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(sql.Identifier(dbname),sql.Identifier(runtime.user)))
            await connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(runtime.user)))
            require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'publication-reference-schema-cleaned')
            require((await (await connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone()) is None,'publication-reference-role-cleaned')

    async def policy_publication(self):
        await self.policy_kernel()
        schema='ref01_probe_'+uuid4().hex;runtime=replace(self.reference_admin,user=schema_and_role(schema),password=secrets.token_urlsafe(48))
        database=None;owned=False;role_oid=schema_oid=None;lifespans=AsyncExitStack()
        try:
            async with direct_connection(self.reference_admin) as connection:
                require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'reference-schema-unowned')
                require((await (await connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone()) is None,'reference-role-unowned')
            await migrate_reference(self.reference_admin,runtime,schema=schema,upgrade=True);owned=True
            async with direct_connection(self.reference_admin) as connection:
                role_oid=(await (await connection.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone())[0]
                schema_oid=(await (await connection.execute('SELECT oid FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone())[0]
                await connection.execute(sql.SQL('INSERT INTO {}.tenant_registry(tenant_id,provisioning_digest,active) VALUES(%s,%s,true)').format(sql.Identifier(schema)),(self.people[0]['tenant'],'b'*64))
            database=ReferenceDatabase(runtime,schema,max_size=2);await database.open()
            signing=SigningFixture();credential=secrets.token_urlsafe(32);dispatcher=secrets.token_urlsafe(32)
            credential_hash=hashlib.sha256(('eai:internal-caller:v1:'+credential).encode()).hexdigest()
            await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'reference-publication-fixture',%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",
                               (uuid4(),credential_hash,AUDIENCE,CALLER,Jsonb(['policy.publish','trips.list','trip.read'])))
            await self.execute("INSERT INTO {s}.delegation_keys VALUES(%s,'current',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(ISSUER,Jsonb(signing.jwk)))
            root=hashlib.sha256(b'eai:policy-settlement:key:v1:'+dispatcher.encode()).hexdigest()
            await self.execute("INSERT INTO {s}.policy_receiver_keys(key_id,key_hash,not_before,not_after) VALUES('actual-reference',%s,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(root,))
            delegation=DelegationService(PostgresDelegationRepository(self.database),RSADelegationVerifier(),ISSUER)
            internal=self.internal_application(delegation)
            await lifespans.enter_async_context(internal.router.lifespan_context(internal))
            with tempfile.TemporaryDirectory(prefix='publication-tls-',dir='local/pol01') as folder:
                cert,key=certificate(Path(folder));trust=ssl.create_default_context(cafile=str(cert))
                async with tls_server(internal,cert,key) as platform_origin:
                    platform_client=PlatformClient(PlatformTarget(platform_origin,credential),transport=httpx.AsyncHTTPTransport(verify=trust))
                    try:
                        reference_app=self.reference_application(database,platform_client,dispatcher,trust)
                        # Drain dependent application clients before shutting down
                        # the TLS server they call. Configured factories allocate
                        # their own clients, unlike injected component services.
                        async with reference_app.router.lifespan_context(reference_app):
                            reference=AckDelivery(reference_app)
                            self.ack_delivery=reference
                            async with tls_server(reference,cert,key) as reference_origin:
                                receiver=PolicyReceiver(PolicyTarget(reference_origin,dispatcher,'actual-reference'),RSACatalogSigner(signing.private,'current',ISSUER,AUDIENCE,CALLER),transport=httpx.AsyncHTTPTransport(verify=trust))
                                trip_reader=TripCatalogReader(PolicyTarget(reference_origin,dispatcher),RSACatalogSigner(signing.private,'current',ISSUER,AUDIENCE,CALLER),transport=httpx.AsyncHTTPTransport(verify=trust))
                                try:
                                    app=self.public_application(receiver,trip_reader,trust)
                                    async with app.router.lifespan_context(app):
                                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url=ORIGIN) as client:
                                            await self.publication_cases(client,database,schema)
                                finally:
                                    try:await trip_reader.close()
                                    finally:await receiver.close()
                    finally:await platform_client.close()
            self.passed('actual_verified_loopback_TLS_platform_and_reference_runtime_complete_with_distinct_restricted_logins_and_owned_cleanup')
        finally:
            async def cleanup():
                try:
                    try:await lifespans.aclose()
                    finally:
                        if database is not None:await database.close()
                finally:
                    if owned:await self.cleanup_reference(schema,runtime,role_oid,schema_oid)
            await finish_cleanup(cleanup())

    async def public_command(self,client,package,operation,version,*,key=None,extra=None,index=0):
        person=self.people[index]
        async with self.database.transaction() as connection:
            binding=(await (await connection.execute(sql.SQL('SELECT {}.browser_context_guard(%s,NULL)').format(sql.Identifier(self.schema)),(digest('session',person['token']),))).fetchone())[0]
        require(binding['status']=='ok','publication-browser-context')
        return await client.post('/api/v1/policy-packages/'+package+'/versions/1/'+operation,
            json=dict(key=key or str(uuid4()),expected_version=version,**(extra or {})),headers={**self.headers(index,'application/json'),'x-eai-expected-context':binding['context_binding']})

    async def publication_cases(self,client,database,schema):
        content={k:v for k,v in self.policy_fixture['content'].items() if k not in {'tenant_id','package_id','package_version'}}
        package=str(uuid4())
        draft=await self.public_command(client,package,'draft',0,extra=dict(content=content));require(draft.status_code==200,'publication-draft-'+str(draft.status_code))
        prepared=await self.public_command(client,package,'prepare',1);require(prepared.status_code==200,'publication-prepare-'+str(prepared.status_code))
        intent=str(uuid4());ack=await self.public_command(client,package,'dispatch',2,key=intent)
        require(ack.status_code==200,'publication-dispatch-'+str(ack.status_code))
        result=ack.json();require(result['state']=='prepared' and result['acknowledged'] is True and result['version']==3,'publication-ack-no-autoactivation')
        require('x-eai-context-binding' in ack.headers and ack.headers.get('cache-control')=='no-store','publication-browser-attestation')
        again=await self.public_command(client,package,'dispatch',2,key=intent)
        require(again.status_code==200 and again.json()['version']==3,'publication-copy-replay-stable')
        async with direct_connection(self.reference_admin) as connection:
            copied=(await (await connection.execute(sql.SQL('SELECT digest,canonical FROM {}.service_policy_packages WHERE package_id=%s').format(sql.Identifier(schema)),(package,))).fetchone())
        require(copied is not None and copied[0]==result['digest'],'reference-exact-copy-persisted')
        original=await self.scalar('SELECT canonical FROM {s}.policy_packages WHERE id=%s',(package,))
        require(copied[1]==original,'reference-independent-immutable-canonical-copy')
        async with database.transaction() as connection:
            isolated=(await (await connection.execute('SELECT has_schema_privilege(current_user,%s,\'USAGE\')',(self.schema,))).fetchone())[0]
            require(isolated is False,'reference-cannot-use-platform-schema')
        async with self.database.transaction() as connection:
            isolated=(await (await connection.execute('SELECT has_schema_privilege(current_user,%s,\'USAGE\')',(schema,))).fetchone())[0]
            require(isolated is False,'platform-cannot-use-reference-schema')
        self.passed('public_browser_guarded_draft_prepare_dispatch_gets_actual_received_ACK_without_activation_and_replay_preserves_exact_independent_copy')
        rejected=await self.public_command(client,package,'activate',3)
        require(rejected.status_code==409,'actual-overlap-activation-rejected')
        require(await self.scalar('SELECT publication_state FROM {s}.policy_packages WHERE id=%s',(package,))=='prepared','overlap-activation-rolled-back')
        old_id=self.policy_fixture['policy']['id'];person=self.people[0]
        day=await self.scalar('SELECT (clock_timestamp() AT TIME ZONE timezone)::date FROM {s}.tenants WHERE id=%s',(person['tenant'],))
        old=await self.scalar('SELECT jsonb_build_object(\'id\',id,\'version\',version) FROM {s}.policy_windows WHERE package_id=%s',(old_id,))
        tenant_version=await self.scalar('SELECT version FROM {s}.policy_coordination WHERE tenant_id=%s',(person['tenant'],))
        changed=await self.public_command(client,package,'supersede',3,extra=dict(old_window_id=old['id'],old_window_version=old['version'],tenant_version=tenant_version,cutoff=(day+timedelta(days=5)).isoformat()))
        require(changed.status_code==200 and changed.json()['state']=='active','actual-future-supersede-'+str(changed.status_code))
        require(await self.scalar('SELECT canonical FROM {s}.policy_packages WHERE id=%s',(old_id,))==json.dumps(self.policy_fixture['content'],sort_keys=True,separators=(',',':'),ensure_ascii=False),'old-package-immutable-after-supersede')
        adjacency=await self.scalar('SELECT a.ends_on=b.starts_on FROM {s}.policy_windows a JOIN {s}.policy_windows b ON b.tenant_id=a.tenant_id WHERE a.package_id=%s AND b.package_id=%s',(old_id,package))
        require(adjacency is True,'actual-supersede-adjacent-windows')
        self.passed('actual_public_activation_overlap_rolls_back_and_future_supersede_preserves_immutable_old_content_with_adjacent_selection_windows')

        async def acknowledged(index=0):
            new_id=str(uuid4())
            require((await self.public_command(client,new_id,'draft',0,extra=dict(content=content),index=index)).status_code==200,'race-draft')
            require((await self.public_command(client,new_id,'prepare',1,index=index)).status_code==200,'race-prepare')
            require((await self.public_command(client,new_id,'dispatch',2,index=index)).status_code==200,'race-actual-copy-ack')
            return new_id
        left=await acknowledged();right=await acknowledged()
        token,csrf=await self.session(person['tenant'],person['person']);self.people.append({**person,'token':token,'csrf':csrf});second=len(self.people)-1
        current=await self.scalar('SELECT jsonb_build_object(\'id\',id,\'version\',version) FROM {s}.policy_windows WHERE package_id=%s',(package,))
        tenant_version=await self.scalar('SELECT version FROM {s}.policy_coordination WHERE tenant_id=%s',(person['tenant'],))
        race_material=dict(old_window_id=current['id'],old_window_version=current['version'],tenant_version=tenant_version,cutoff=(day+timedelta(days=10)).isoformat())
        async with asyncio.timeout(8):
            race=await asyncio.gather(self.public_command(client,left,'supersede',3,extra=race_material),self.public_command(client,right,'supersede',3,extra=race_material,index=second))
        require(sorted(r.status_code for r in race)==[200,409],'actual-two-session-supersede-race')
        require(await self.scalar('SELECT count(*) FROM {s}.policy_windows a JOIN {s}.policy_windows b ON a.tenant_id=b.tenant_id AND a.id<b.id WHERE daterange(a.starts_on,a.ends_on,\'[)\') && daterange(b.starts_on,b.ends_on,\'[)\')')==0,'race-no-window-overlap')
        require(await self.scalar('SELECT count(*) FROM {s}.policy_packages WHERE id::text=ANY(%s) AND publication_state=\'active\'',([left,right],))==1,'race-one-winner')
        self.passed('actual_supersede_commands_from_distinct_sessions_with_same_tenant_window_CAS_accept_exactly_one_and_leave_no_overlap')

        # The copy commits, but the ACK is delayed past the sender read timeout.
        # Retrying the same durable intent must retrieve the same received copy,
        # rather than create another package/receipt or activate anything.
        delayed_id=str(uuid4());delayed_key=str(uuid4());committed=asyncio.Event()
        require((await self.public_command(client,delayed_id,'draft',0,extra=dict(content=content))).status_code==200,'timeout-copy-draft')
        require((await self.public_command(client,delayed_id,'prepare',1)).status_code==200,'timeout-copy-prepare')
        async def delay_ack():
            committed.set();await asyncio.sleep(4.5)
        self.ack_delivery.after_success=delay_ack
        missing=await self.public_command(client,delayed_id,'dispatch',2,key=delayed_key)
        require(committed.is_set() and missing.status_code==503,'real-copy-committed-before-ack-timeout')
        require(await self.scalar('SELECT service_ack_digest IS NULL AND publication_state=\'prepared\' FROM {s}.policy_packages WHERE id=%s',(delayed_id,)) is True,'timeout-copy-local-inactive-without-ack')
        recovered=await self.public_command(client,delayed_id,'dispatch',2,key=delayed_key)
        require(recovered.status_code==200 and recovered.json()['state']=='prepared' and recovered.json()['version']==3,'actual-lost-ack-stable-intent-recovery')
        async with direct_connection(self.reference_admin) as connection:
            copies=(await (await connection.execute(sql.SQL('SELECT count(*) FROM {}.service_policy_packages WHERE package_id=%s').format(sql.Identifier(schema)),(delayed_id,))).fetchone())[0]
            receipts=(await (await connection.execute(sql.SQL('SELECT count(*) FROM {}.package_receipts WHERE package_id=%s').format(sql.Identifier(schema)),(delayed_id,))).fetchone())[0]
        require(copies==receipts==1,'lost-ack-no-duplicate-copy-or-receipt')
        self.passed('actual_TLS_ACK_timeout_after_remote_commit_recovers_same_durable_intent_with_one_copy_receipt_and_no_autoactivation')

        # A publisher loses its role after the real service copy committed. The
        # returned ACK must not settle under the old session/context epoch.
        pid=uuid4();mid=await self.member(person['tenant'],pid,grants=(RoleGrant('employee','tenant',str(person['tenant'])),RoleGrant('knowledge_manager','tenant',str(person['tenant'])),RoleGrant('process_owner','process','reimbursement')))
        token,csrf=await self.session(person['tenant'],pid);self.people.append(dict(tenant=person['tenant'],person=pid,member=mid,token=token,csrf=csrf));publisher=len(self.people)-1
        revoked_id=str(uuid4())
        require((await self.public_command(client,revoked_id,'draft',0,extra=dict(content=content),index=publisher)).status_code==200,'revoked-publisher-draft')
        require((await self.public_command(client,revoked_id,'prepare',1,index=publisher)).status_code==200,'revoked-publisher-prepare')
        async def revoke_publisher():
            await self.execute("DELETE FROM {s}.membership_roles WHERE membership_id=%s AND role='process_owner'",(mid,))
        self.ack_delivery.after_success=revoke_publisher
        lost=await self.public_command(client,revoked_id,'dispatch',2,index=publisher)
        require(lost.status_code in (401,403),'actual-publisher-revocation-denies-ack-'+str(lost.status_code))
        require(await self.scalar('SELECT service_ack_digest IS NULL AND publication_state=\'prepared\' FROM {s}.policy_packages WHERE id=%s',(revoked_id,)) is True,'lost-publisher-no-ack')
        self.passed('publisher_role_revoked_after_actual_remote_commit_before_ACK_delivery_cannot_settle_or_activate_local_package')

        source_id=str(uuid4())
        require((await self.public_command(client,source_id,'draft',0,extra=dict(content=content))).status_code==200,'revoked-source-draft')
        require((await self.public_command(client,source_id,'prepare',1)).status_code==200,'revoked-source-prepare')
        async def withdraw_source():
            async with asyncio.timeout(3):response=await self.mutate(self.policy_fixture['source'],'withdraw')
            require(response.status_code==200,'source-withdraw-during-network-no-held-db-lock')
        self.ack_delivery.after_success=withdraw_source
        unavailable=await self.public_command(client,source_id,'dispatch',2)
        require(unavailable.status_code==409,'actual-source-withdrawal-denies-ack-'+str(unavailable.status_code))
        require(await self.scalar('SELECT service_ack_digest IS NULL AND publication_state=\'prepared\' FROM {s}.policy_packages WHERE id=%s',(source_id,)) is True,'lost-source-no-ack')
        async with direct_connection(self.reference_admin) as connection:
            copied_count=(await (await connection.execute(sql.SQL('SELECT count(*) FROM {}.service_policy_packages WHERE package_id::text=ANY(%s)').format(sql.Identifier(schema)),([revoked_id,source_id],))).fetchone())[0]
        require(copied_count==2,'revoked-ACK-remote-copies-remain-independent')
        self.passed('source_withdrawn_via_actual_knowledge_API_during_ACK_wait_completes_without_held_DB_locks_and_blocks_local_settlement_after_remote_commit')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/pol01/publication-proof.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned disposable database proof.')
    try:
        paths=DRAFTS+[Path('scripts/checks/check_policy_draft.py'),Path('scripts/checks/check_reference_actor.py'),Path(__file__)]+list(Path('reference_service/src').rglob('*.py'))+list(Path('reference_service/src').rglob('*.sql'))
        snapshots={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_admin=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,harness_type=Harness,target_version=21,operation='policy_publication',direct_timeout=240))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in snapshots.items()),'publication-source-stable')
        result.update(scope='policy_TLS_publication_component_not_whole_acceptance',draft_sha256=snapshots,service_revision=3,database_isolation='same existing PostgreSQL database; separate owned schemas and restricted logins',public_api_transport='ASGI with browser guard; both interservice directions use actual verified TLS loopback sockets',runtime_composition='explicit test fixture, not production startup configuration')
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','draft_sha256')}))
    except Exception as error:
        print('Policy publication proof failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None


if __name__=='__main__':main()
