"""POL-01 date/subset and hold commands through configured APIs/TLS/owned DB."""
import argparse
import asyncio
import copy
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from check_policy_configured_runtime import Harness as ConfiguredHarness
from check_policy_publication import Target, DRAFTS
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require, CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target, verify_same_database
from enterprise_ai_platform.knowledge_check import write_json


class Harness(ConfiguredHarness):
    async def publication_cases(self, client, database, schema):
        self.configured_phase = 'policy_date_and_hold_commands'
        require(all(a.state.resources.readiness.ready for a in self.configured_apps), 'edge-configured-readiness')
        person = self.people[0]; tenant = person['tenant']
        day = await self.scalar('SELECT (clock_timestamp() AT TIME ZONE timezone)::date FROM {s}.tenants WHERE id=%s', (tenant,))
        old_id = self.policy_fixture['policy']['id']
        old = await self.scalar("SELECT jsonb_build_object('id',id,'version',version) FROM {s}.policy_windows WHERE package_id=%s", (old_id,))
        revision = await self.scalar('SELECT version FROM {s}.policy_coordination WHERE tenant_id=%s', (tenant,))
        original = await self.scalar('SELECT canonical FROM {s}.policy_packages WHERE id=%s', (old_id,))
        content = copy.deepcopy({k:v for k,v in self.policy_fixture['content'].items()
                                 if k not in {'tenant_id','package_id','package_version'}})
        content.update(valid_trip_end_from=(day+timedelta(days=10)).isoformat(),
                       valid_trip_end_to=(day+timedelta(days=20)).isoformat())
        content['rules']['submission_deadline_days'] = 30

        async def prepared(body):
            package = str(uuid4())
            for operation, version, extra in [('draft',0,{'content':body}),('prepare',1,None),('dispatch',2,None)]:
                response = await self.public_command(client, package, operation, version, extra=extra)
                require(response.status_code==200, 'edge-'+operation+'-'+str(response.status_code))
            return package

        # Separate candidate includes today, so the future-only guard is tested
        # independently of immutable-range subset rejection.
        broad=copy.deepcopy(content)
        broad.update(valid_trip_end_from=self.policy_fixture['content']['valid_trip_end_from'],valid_trip_end_to=None)
        broad_package=await prepared(broad)
        package = await prepared(content)
        material = dict(old_window_id=old['id'], old_window_version=old['version'], tenant_version=revision)
        async def windows():
            return await self.scalar("SELECT coalesce(jsonb_agg(to_jsonb(w) ORDER BY id),'[]'::jsonb) FROM {s}.policy_windows w WHERE tenant_id=%s", (tenant,))
        before = await windows()
        audit_before=await self.scalar('SELECT count(*) FROM {s}.policy_audit WHERE tenant_id=%s',(tenant,))
        for cutoff in (day-timedelta(days=1),day):
            response=await self.public_command(client,broad_package,'supersede',3,
                extra=dict(**material,cutoff=cutoff.isoformat()))
            require(response.status_code==409,'edge-independent-future-only-cutoff')
            require(await windows()==before,'edge-current-day-no-window-write')
            require(await self.scalar('SELECT version FROM {s}.policy_coordination WHERE tenant_id=%s',(tenant,))==revision,'edge-current-day-no-coordination-write')
            require(await self.scalar('SELECT count(*) FROM {s}.policy_audit WHERE tenant_id=%s',(tenant,))==audit_before,'edge-current-day-no-audit-write')
        self.passed('configured_acknowledged_range_covering_today_and_past_still_rejects_nonfuture_supersession_without_window_coordination_or_audit_writes')
        # Same/current tenant business date, before the immutable new range, and
        # the exclusive new upper bound all fail without changing either window.
        for cutoff in (day, day+timedelta(days=5), day+timedelta(days=20), day+timedelta(days=21)):
            response = await self.public_command(client, package, 'supersede', 3,
                extra=dict(**material, cutoff=cutoff.isoformat()))
            require(response.status_code==409, 'edge-invalid-cutoff-'+str(response.status_code))
            require(await windows()==before, 'edge-cutoff-no-window-partial-write')
            require(await self.scalar('SELECT version FROM {s}.policy_coordination WHERE tenant_id=%s',(tenant,))==revision, 'edge-cutoff-no-coordination-advance')
        self.passed('configured_supersession_rejects_current_tenant_day_and_outside_immutable_subset_including_exclusive_upper_bound_atomically')

        cutoff=day+timedelta(days=10); key=str(uuid4())
        response=await self.public_command(client,package,'supersede',3,key=key,extra=dict(**material,cutoff=cutoff.isoformat()))
        require(response.status_code==200 and response.json()['version']==4,'edge-valid-start-boundary-supersession')
        selected=await windows()
        replay=await self.public_command(client,package,'supersede',3,key=key,extra=dict(**material,cutoff=cutoff.isoformat()))
        stable=lambda value:{k:v for k,v in value.items() if k!='authority_expires_at'}
        require(replay.status_code==200 and stable(replay.json())==stable(response.json()) and await windows()==selected,'edge-supersession-replay-no-second-narrow')
        require(await self.scalar("SELECT count(*) FROM {s}.policy_audit WHERE package_id=%s AND operation='supersede'",(package,))==1,'edge-replay-one-audit')
        for date, expected in [(day+timedelta(days=9),old_id),(cutoff,package),(day+timedelta(days=19),package),(day+timedelta(days=20),None)]:
            found=await self.scalar("SELECT (SELECT package_id::text FROM {s}.policy_windows WHERE tenant_id=%s AND starts_on<=%s AND (ends_on IS NULL OR ends_on>%s))",(tenant,date,date))
            require(found==expected,'edge-half-open-exact-date-selection')
        require(await self.scalar('SELECT canonical FROM {s}.policy_packages WHERE id=%s',(old_id,))==original,'edge-old-canonical-unchanged')
        require(json.loads(original)['rules']['submission_deadline_days']==7,'edge-old-deadline-not-replaced')
        self.passed('configured_future_supersession_accepts_new_start_preserves_old_mapping_deadline_and_exact_replay_with_half_open_upper_bound')

        # Timezone projection vectors around midnight; no fake product clock or
        # claim that a live request waited across midnight.
        require(await self.scalar("SELECT (((timestamp '2026-06-02 00:00:00' AT TIME ZONE timezone) - interval '1 microsecond') AT TIME ZONE timezone)::date < ((timestamp '2026-06-02 00:00:00' AT TIME ZONE timezone) AT TIME ZONE timezone)::date FROM {s}.tenants WHERE id=%s",(tenant,)) is True,'edge-tenant-midnight-projection')
        wrong=copy.deepcopy(content);wrong['timezone']='UTC' if content['timezone']!='UTC' else 'Asia/Singapore'
        wrong_id=str(uuid4())
        require((await self.public_command(client,wrong_id,'draft',0,extra={'content':wrong})).status_code==200,'edge-timezone-draft')
        require((await self.public_command(client,wrong_id,'prepare',1)).status_code==409,'edge-wrong-tenant-timezone-denied')
        # Give tenant B its own legitimate publisher and readable published source.
        # Temporarily select fixture owner B for the existing upload/worker helper;
        # production authorization, repositories and ingestion remain unchanged.
        foreign=self.people[2]
        await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(%s,%s,%s,'process_owner','process','reimbursement',%s)",
            (foreign['tenant'],uuid4(),foreign['member'],foreign['person']))
        foreign['token'],foreign['csrf']=await self.session(foreign['tenant'],foreign['person'])
        owner,jobs,inspector=self.people[0],self.jobs,self.inspector
        try:
            self.people[0]=foreign
            self.jobs=await self.caller('executor',foreign['tenant'])
            self.inspector=type(inspector)(self.file_repo,self.store,self.parser,self.jobs,self.jobs._credential_hash)
            file_id=await self.ready_file('Fictional tenant B policy.')
            admitted=await self.create_document(file_id,readers=[str(foreign['person'])])
            require(admitted.status_code==202,'edge-B-source-admission')
            b_source=admitted.json();await self.process_knowledge()
            require((await self.mutate(b_source,'publish')).status_code==200,'edge-B-source-published')
        finally:
            self.people[0],self.jobs,self.inspector=owner,jobs,inspector
        b_body=copy.deepcopy(broad)
        b_body['timezone']=await self.scalar('SELECT timezone FROM {s}.tenants WHERE id=%s',(foreign['tenant'],))
        b_body['document_versions']=[dict(document_id=b_source['document_id'],version_id=b_source['version_id'],
            content_digest=await self.scalar('SELECT content_digest FROM {s}.document_versions WHERE id=%s',(b_source['version_id'],)))]
        b_package=str(uuid4())
        require((await self.public_command(client,b_package,'draft',0,index=2,extra={'content':b_body})).status_code==200,'edge-B-authorized-publisher-draft')
        require((await self.public_command(client,b_package,'prepare',1,index=2)).status_code==200,'edge-B-authorized-publisher-source-read')
        require((await self.public_command(client,package,'retire',4,index=2)).status_code==404,'edge-foreign-tenant-authorized-publisher-denied')
        require(await windows()==selected,'edge-foreign-tenant-no-window-write')
        self.passed('configured_commands_require_tenant_timezone_and_same_tenant_authority_with_explicit_midnight_projection_vectors')

        package_canonical=await self.scalar('SELECT canonical FROM {s}.policy_packages WHERE id=%s',(package,))
        retired=await self.public_command(client,package,'retire',4)
        require(retired.status_code==200 and retired.json()['version']==5,'edge-actual-retire')
        require(await self.scalar('SELECT NOT allowed_for_new AND allowed_for_locked_claims FROM {s}.policy_packages WHERE id=%s',(package,)) is True,'edge-retire-preserves-locked-use')
        require(await windows()==selected,'edge-retire-keeps-selection-window')
        overlap=await prepared(content)
        require((await self.public_command(client,overlap,'activate',3)).status_code==409,'edge-retired-window-not-free-for-overlap')
        conflict=await self.public_command(client,package,'conflict',5)
        require(conflict.status_code==200 and conflict.json()['version']==6,'edge-actual-conflict')
        require(await self.scalar("SELECT NOT allowed_for_new AND NOT allowed_for_locked_claims AND blocked_reason='known_conflict' FROM {s}.policy_packages WHERE id=%s",(package,)) is True,'edge-conflict-holds-all-use')
        withdrawn=await self.public_command(client,package,'withdraw',6)
        require(withdrawn.status_code==200 and withdrawn.json()['version']==7,'edge-actual-withdraw')
        require(await self.scalar("SELECT publication_state='withdrawn' AND NOT allowed_for_new AND NOT allowed_for_locked_claims FROM {s}.policy_packages WHERE id=%s",(package,)) is True,'edge-withdraw-holds-all-use')
        require(await windows()==selected,'edge-holds-never-free-historical-window')
        require(await self.scalar('SELECT canonical FROM {s}.policy_packages WHERE id=%s',(package,))==package_canonical,'edge-holds-preserve-immutable-package-canonical')
        self.passed('configured_actual_retire_conflict_and_withdraw_commands_preserve_historical_windows_and_retirement_does_not_permit_overlapping_activation')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned database fixture.')
    paths=list(dict.fromkeys(DRAFTS+[Path('scripts/checks')/p for p in [
        'check_policy_draft.py','check_policy_publication.py','check_trip_catalog.py',
        'check_reference_actor.py','check_policy_configured_runtime.py','check_policy_edges.py']]
        +list(Path('reference_service/src').rglob('*.py'))+list(Path('reference_service/src').rglob('*.sql'))))
    snapshots={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    try:
        settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_admin=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,harness_type=Harness,target_version=21,operation='policy_publication',direct_timeout=240))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in snapshots.items()),'edge-source-stable')
        result.update(scope='configured_policy_date_and_hold_commands; not actual claim producer or midnight-wait simulation',
            draft_sha256=snapshots,service_revision=3,actual_configured_factories=True)
        write_json(Path('local/pol01/edges-proof.json'),result)
        print(json.dumps({k:v for k,v in result.items() if k not in ('draft_sha256','source_sha256')}))
    except Exception as error:
        print('Policy edge proof failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'))
        raise SystemExit(1) from None


if __name__=='__main__':main()
