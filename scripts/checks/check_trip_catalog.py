"""POL-01 actual employee trips.list producer through verified TLS + both DB roles."""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import sys
from uuid import uuid4
from psycopg import sql
from check_policy_publication import Harness as PublicationHarness,Target,direct_connection,DRAFTS
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from reimbursement_service.modules.trips.domain import Trip,ExpenseItem
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'reference_service/scripts'))
from seed_reference_fixture import seed_trips


class Harness(PublicationHarness):
    async def trip_get(self,client,*,index=0,after=None,limit=2):
        person=self.people[index]
        async with self.database.transaction() as connection:
            binding=(await (await connection.execute(sql.SQL('SELECT {}.browser_context_guard(%s,NULL)').format(sql.Identifier(self.schema)),(digest('session',person['token']),))).fetchone())[0]
        require(binding['status']=='ok','trip-browser-context')
        params=dict(limit=limit)
        if after is not None:params['after']=after
        return await client.get('/api/v1/trips',params=params,headers={**self.headers(index),'x-eai-expected-context':binding['context_binding']})

    async def publication_cases(self,client,database,schema):
        # The inherited harness provides real TLS apps/catalog verifier/serviceDB;
        # this override exercises the list producer, not package publication.
        def trip():
            return Trip(str(uuid4()),'2026-10-01','2026-10-03','completed','Fictional traveler',
                (ExpenseItem(str(uuid4()),'2026-10-02','transport',500,'SGD'),))
        own=sorted([trip() for _ in range(4)],key=lambda t:t.id);colleague=trip();foreign=trip()
        async with direct_connection(self.reference_admin) as connection:
            await connection.execute(sql.SQL('INSERT INTO {}.tenant_registry(tenant_id,provisioning_digest,active) VALUES(%s,%s,true)').format(sql.Identifier(schema)),(self.people[2]['tenant'],'c'*64))
            for person,values in ((self.people[0],tuple(own)),(self.people[1],(colleague,)),(self.people[2],(foreign,))):
                await seed_trips(connection,schema,str(person['tenant']),str(person['person']),values)
        first=await self.trip_get(client);require(first.status_code==200,'actual-trip-list-'+str(first.status_code))
        require([t['id'] for t in first.json()['items']]==[t.id for t in own[:2]] and first.json()['next_cursor']==own[1].id,'exact-owner-first-page')
        require(all(set(t)=={'id','start_date','end_date','state','employee_ref'} for t in first.json()['items']),'no-expense-item-or-amount-in-list')
        require(first.headers.get('cache-control')=='no-store' and 'x-eai-context-binding' in first.headers,'trip-context-attestation')
        second=await self.trip_get(client,after=first.json()['next_cursor'])
        require(second.status_code==200 and [t['id'] for t in second.json()['items']]==[t.id for t in own[2:]] and second.json()['next_cursor'] is None,'exact-owner-next-page')
        self.passed('actual_employee_browser_list_producer_signed_collection_v2_over_TLS_returns_only_owner_summaries_with_scoped_cursor_and_context_attestation')
        for cursor in (colleague.id,foreign.id,str(uuid4())):
            require((await self.trip_get(client,after=cursor)).status_code==404,'foreign-or-missing-trip-cursor-hidden')
        require([t['id'] for t in (await self.trip_get(client,index=1)).json()['items']]==[colleague.id],'colleague-exact-owned-trip')
        require([t['id'] for t in (await self.trip_get(client,index=2)).json()['items']]==[foreign.id],'foreign-tenant-exact-owned-trip')
        self.passed('real_reference_trips_cursor_and_rows_isolate_same_tenant_colleagues_and_other_tenants')
        admin=await self.trip_get(client,index=3)
        require(admin.status_code==403,'tenant-admin-does-not-inherit-employee-list-authority')
        missing=await client.get('/api/v1/trips',headers=self.headers())
        require(missing.status_code==400 and missing.headers.get('cache-control')=='no-store','expected-context-required-for-trip-read')
        self.passed('admin_without_employee_role_and_browser_missing_expected_context_cannot_read_catalog')

        # Membership-role revision is changed after the actual reference DB read,
        # before the list is delivered. A fresh post-network guard must withhold
        # the already returned remote data under the stale session epoch.
        tenant=self.people[0]['tenant'];person=uuid4();member=await self.member(tenant,person,grants=(RoleGrant('employee','tenant',str(tenant)),))
        token,csrf=await self.session(tenant,person);self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf));index=len(self.people)-1
        private_trip=trip()
        async with direct_connection(self.reference_admin) as connection:await seed_trips(connection,schema,str(tenant),str(person),(private_trip,))
        changed=asyncio.Event()
        async def revoke():
            async with asyncio.timeout(3):await self.execute("DELETE FROM {s}.membership_roles WHERE membership_id=%s AND role='employee'",(member,))
            changed.set()
        self.ack_delivery.after_success=revoke
        revoked=await self.trip_get(client,index=index)
        require(changed.is_set() and revoked.status_code in (401,403,409),'trip-inflight-current-authority-recheck-'+str(revoked.status_code))
        require(private_trip.id not in revoked.text and 'Fictional traveler' not in revoked.text,'inflight-revocation-no-partial-data-leak')
        self.passed('employee_role_revoked_after_actual_remote_read_is_rechecked_after_network_and_response_withheld_without_held_platform_locks')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/pol01/trip-catalog-proof.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned disposable database proof.')
    try:
        paths=DRAFTS+[Path('scripts/checks/check_policy_draft.py'),Path('scripts/checks/check_policy_publication.py'),Path('scripts/checks/check_reference_actor.py'),Path('reference_service/scripts/seed_reference_fixture.py'),Path(__file__)]+list(Path('reference_service/src').rglob('*.py'))+list(Path('reference_service/src').rglob('*.sql'))
        snapshots={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_admin=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,harness_type=Harness,target_version=21,operation='policy_publication',direct_timeout=240))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in snapshots.items()),'trip-catalog-source-stable')
        result.update(scope='actual_trips_list_producer_component_not_whole_POL_acceptance',draft_sha256=snapshots,service_revision=3,transport='public ASGI browser guarded; interservice certificate-verified TLS loopback',database_isolation='one existing physical PostgreSQL database; distinct owned schemas and restricted logins',fixture='fictional operator-seeded trips; actual authority producer/verification/repos')
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','draft_sha256')}))
    except Exception as error:
        print('Trip catalog proof failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None


if __name__=='__main__':main()
