"""JOB-01 opt-in real PostgreSQL races, isolation, retries and outbox rollback."""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import sys
from uuid import UUID, uuid4

from psycopg import sql
from enterprise_ai_platform.authorization_check import Harness as AccessHarness, CheckFailure, require
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, direct_connection, parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role, drop_probe_schema
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.modules.jobs.domain import JobRequest, Lease
from enterprise_ai_platform.modules.jobs.ports import JobFailure
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.modules.identity.service import digest


class Harness(AccessHarness):
    def __init__(self, admin, database, schema, direct, target):
        super().__init__(admin, database, schema)
        self.direct = direct
        self.target = target
        self.repo_authorization = self.repo
        self.repo = PostgresJobRepository(database)
        self.checks = []
        self.tenants = []

    def passed(self, name):
        require(name not in self.checks, 'duplicate-check')
        self.checks.append(name)

    async def scalar(self, query, args=None):
        return (await (await self.execute(query, args)).fetchone())[0]

    async def caller(self, kind, tenant=None):
        credential = secrets.token_urlsafe(32)
        service = JobService(self.repo, credential)
        caller_id = uuid4()
        await self.execute('INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,%s,%s,clock_timestamp()+interval \'1 day\')',
                           (caller_id, service._credential_hash, kind, tenant))
        return caller_id, service, credential

    async def setup_jobs(self):
        for _ in range(5):
            tenant, actor = uuid4(), uuid4()
            member = await self.member(tenant, actor)
            token, _ = await self.session(tenant, actor)
            session = await self.scalar('SELECT id FROM {s}.sessions WHERE token_hash=%s', (digest('session', token),))
            revision = await self.scalar('SELECT access_revision FROM {s}.memberships WHERE tenant_id=%s AND id=%s', (tenant, member))
            _, producer, _ = await self.caller('producer', tenant)
            worker_id, worker, secret = await self.caller('executor', tenant)
            self.tenants.append(dict(tenant=tenant, actor=actor, member=member, session=session, revision=revision, token=token,
                                     producer=producer, worker=worker, worker_id=worker_id, secret=secret))
        self.scheduler_id, self.scheduler, _ = await self.caller('scheduler')

    def request(self, n=0, **kwargs):
        t = self.tenants[n]
        values = dict(id=uuid4(), job_type='agent.resume', business_key=uuid4(), target_id=uuid4(), expected_version=1,
                      actor_id=t['actor'], membership_id=t['member'], session_id=t['session'], access_revision=t['revision'])
        return JobRequest(**{**values, **kwargs})

    async def reset_jobs(self):
        await self.execute('DELETE FROM {s}.outbox_jobs; DELETE FROM {s}.job_tenants; UPDATE {s}.job_scheduler SET turn=0')

    async def rejected(self, name, awaitable, status):
        try:
            await awaitable
        except JobFailure as error:
            require(error.status == status, name+'-status-'+error.status)
        else:
            raise CheckFailure(name+'-accepted')
        self.passed(name)

    def lease(self, claim):
        return Lease(UUID(claim['id']), UUID(claim['tenant_id']), claim['generation'])

    def executor(self, claim):
        return next(t['worker'] for t in self.tenants if str(t['worker_id']) == claim['worker_id'])

    async def snapshot(self):
        return await self.scalar('SELECT coalesce(jsonb_agg(to_jsonb(j) ORDER BY tenant_id,id),\'[]\') FROM {s}.outbox_jobs j')

    async def boundary_checks(self):
        request = self.request()
        unknown = JobService(self.repo, secrets.token_urlsafe(32))
        await self.rejected('unknown_caller_rejected', unknown.claim(), 'unauthenticated')
        await self.rejected('executor_cannot_enqueue', self.tenants[0]['worker'].enqueue(request), 'forbidden')
        await self.rejected('producer_cannot_claim', self.tenants[0]['producer'].claim(), 'forbidden')
        await self.rejected('scheduler_cannot_enqueue', self.scheduler.enqueue(request), 'forbidden')
        await self.tenants[0]['producer'].enqueue(request)
        claim = await self.scheduler.claim()
        lease = self.lease(claim)
        require(set(claim) == {'status','id','tenant_id','job_type','worker_id','generation','lease_until','attempts'}, 'claim-metadata-shape')
        self.passed('scheduler_receives_metadata_only')
        await self.rejected('scheduler_cannot_read_reference', self.scheduler.reference(lease), 'forbidden')
        await self.rejected('producer_cannot_read_reference', self.tenants[0]['producer'].reference(lease), 'forbidden')
        await self.rejected('cross_tenant_executor_cannot_read_reference', self.tenants[1]['worker'].reference(lease), 'stale')
        await self.rejected('cross_tenant_inspection_missing', self.tenants[1]['worker'].inspect(request.id), 'missing')
        reference = await self.tenants[0]['worker'].reference(lease)
        require(reference['authority_scope'] == 'actor_only' and reference['target_id'] == str(request.target_id), 'reference-binding')
        self.passed('executor_reference_bound_to_actor_and_target_not_business_permission')
        other_id, other, _ = await self.caller('executor', self.tenants[0]['tenant'])
        await self.rejected('same_tenant_other_worker_cannot_read_reference', other.reference(lease), 'stale')
        await self.execute('DELETE FROM {s}.job_callers WHERE id=%s', (other_id,))
        for table in ('job_callers','job_scheduler','job_tenants','outbox_jobs'):
            try:
                async with self.database.transaction() as connection:
                    await connection.execute(sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(self.schema),sql.Identifier(table)))
            except Exception:
                pass
            else:
                raise CheckFailure('private-table-readable')
            self.passed('restricted_runtime_cannot_select_'+table)
        await self.execute('UPDATE {s}.job_callers SET active=false WHERE id=%s', (self.tenants[0]['worker_id'],))
        await self.rejected('revoked_worker_rejected', self.tenants[0]['worker'].heartbeat(lease), 'unauthenticated')
        await self.execute('UPDATE {s}.job_callers SET active=true,expires_at=clock_timestamp()-interval \'1 second\' WHERE id=%s', (self.tenants[0]['worker_id'],))
        await self.rejected('expired_worker_rejected', self.tenants[0]['worker'].reference(lease), 'unauthenticated')
        await self.execute('UPDATE {s}.job_callers SET expires_at=clock_timestamp()+interval \'1 day\' WHERE id=%s', (self.tenants[0]['worker_id'],))
        await self.reset_jobs()

    async def outbox_checks(self):
        t = self.tenants[0]
        first = self.request()
        other = replace(first,id=uuid4())
        a, b = await asyncio.gather(t['producer'].enqueue(first), t['producer'].enqueue(other))
        require(a['id'] == b['id'] and a['id'] in {str(first.id),str(other.id)} and sorted([a['replayed'],b['replayed']]) == [False,True], 'concurrent-idempotence')
        self.passed('concurrent_enqueue_same_identity_one_durable_job')
        for field, value in (('target_id',uuid4()),('expected_version',2),('retry_limit',2),('access_revision',2)):
            # Revision mismatch is rejected as authority invalid even before identity matching.
            await self.rejected('same_key_changed_'+field, t['producer'].enqueue(replace(first,**{field:value})), 'forbidden' if field=='access_revision' else 'conflict')
        changed = replace(first, id=uuid4(), job_type='action.dispatch')
        await t['producer'].enqueue(changed)
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs') == 2, 'typed-business-key')
        self.passed('business_identity_scoped_by_job_type')
        await self.reset_jobs()
        rollback = self.request()
        try:
            async with self.database.transaction() as connection:
                service = JobService(PostgresJobRepository(self.database,connection=connection), 'x'*43)
                service._credential_hash = t['producer']._credential_hash
                await service.enqueue(rollback)
                raise CheckFailure('intentional-rollback')
        except CheckFailure:
            pass
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs') == 0, 'enqueue-rollback')
        self.passed('enqueue_shares_and_rolls_back_with_callers_transaction')
        async with self.database.transaction() as connection:
            service = JobService(PostgresJobRepository(self.database,connection=connection), 'x'*43)
            service._credential_hash = t['producer']._credential_hash
            await service.enqueue(rollback)
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs') == 1, 'enqueue-commit')
        self.passed('enqueue_commits_with_callers_transaction')
        await self.reset_jobs()

    async def claim_checks(self):
        t = self.tenants[0]
        first = self.request()
        await t['producer'].enqueue(first)
        results = await asyncio.gather(self.scheduler.claim(), self.scheduler.claim())
        require(sorted(r['status'] for r in results) == ['empty','ok'], 'concurrent-claim')
        claim = next(r for r in results if r['status']=='ok')
        require(claim['generation']==1 and claim['attempts']==1, 'generation-first')
        self.passed('two_schedulers_claim_once_with_attempt_and_generation')
        restarted = JobService(PostgresJobRepository(self.database), t['secret'])
        await restarted.reference(self.lease(claim))
        self.passed('worker_restart_reads_durable_lease')
        before = await self.scalar('SELECT attempt_deadline FROM {s}.outbox_jobs')
        await restarted.heartbeat(self.lease(claim))
        require(await self.scalar('SELECT lease_until<=attempt_deadline FROM {s}.outbox_jobs') and before==await self.scalar('SELECT attempt_deadline FROM {s}.outbox_jobs'), 'heartbeat-bound')
        self.passed('heartbeat_cannot_extend_attempt_deadline')
        await self.execute('UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval \'1 second\'')
        snapshot = await self.snapshot()
        for op in ('reference','heartbeat','complete'):
            await self.rejected('expired_lease_rejects_'+op, getattr(restarted,op)(self.lease(claim)), 'stale')
        await self.rejected('expired_lease_rejects_failure_write', restarted.fail(self.lease(claim),'transient'), 'stale')
        require(await self.snapshot()==snapshot, 'stale-no-mutations')
        self.passed('expired_worker_leaves_job_exactly_unchanged')
        require((await self.scheduler.claim())['status']=='empty', 'expiry-backoff')
        require(await self.scalar("SELECT state='retry' AND error_code='lease_expired' AND next_attempt_at>clock_timestamp() FROM {s}.outbox_jobs"), 'expired-retry')
        self.passed('lease_expiry_consumes_attempt_and_obeys_backoff')
        await self.execute('UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp()-interval \'1 second\'')
        second = await self.scheduler.claim()
        require(second['id']==claim['id'] and second['generation']==2 and second['attempts']==2, 'takeover')
        self.passed('takeover_increments_generation_preserving_job_identity')
        snapshot = await self.snapshot()
        for op in ('reference','heartbeat','complete'):
            await self.rejected('stale_generation_rejects_'+op, getattr(restarted,op)(self.lease(claim)), 'stale')
        await self.rejected('stale_generation_cannot_retry_or_hold', restarted.fail(self.lease(claim),'invalid'), 'stale')
        require(await self.snapshot()==snapshot,'old-worker-no-writes')
        self.passed('returning_old_worker_cannot_write_after_takeover')
        await restarted.complete(self.lease(second))
        require((await restarted.inspect(first.id))['state']=='completed','completed-state')
        await self.rejected('completed_generation_cannot_be_reused', restarted.heartbeat(self.lease(second)), 'stale')
        replay = await t['producer'].enqueue(first)
        require(replay['id']==str(first.id) and replay['replayed'] and (await self.scheduler.claim())['status']=='empty', 'completed-replay')
        self.passed('completed_business_identity_never_reenqueues')
        await self.reset_jobs()

    async def retry_checks(self):
        t = self.tenants[0]
        for code in ('invalid','forbidden','unknown_commit'):
            request = self.request()
            await t['producer'].enqueue(request)
            claim = await self.scheduler.claim()
            await t['worker'].fail(self.lease(claim),code)
            require((await t['worker'].inspect(request.id))['state']=='held' and (await self.scheduler.claim())['status']=='empty', 'permanent-held-'+code)
            self.passed(code+'_held_without_automatic_retry_or_new_identity')
            await self.reset_jobs()
        for code in ('transient','rate_limited'):
            request = self.request(retry_limit=2)
            await t['producer'].enqueue(request)
            first = await self.scheduler.claim()
            await t['worker'].fail(self.lease(first),code)
            require(await self.scalar("SELECT state='retry' AND extract(epoch FROM next_attempt_at-updated_at) BETWEEN 2 AND 2.5 FROM {s}.outbox_jobs"), 'backoff-jitter')
            require((await self.scheduler.claim())['status']=='empty', 'backoff-not-due')
            await self.execute('UPDATE {s}.outbox_jobs SET next_attempt_at=clock_timestamp()-interval \'1 second\'')
            second = await self.scheduler.claim()
            require(second['generation']==2 and second['id']==first['id'], 'retry-generation')
            await t['worker'].fail(self.lease(second),code)
            require((await t['worker'].inspect(request.id))['state']=='dead' and (await self.scheduler.claim())['status']=='empty','retry-budget')
            self.passed(code+'_backoff_jitter_and_durable_retry_budget')
            await self.reset_jobs()
        request = self.request(retry_limit=1)
        await t['producer'].enqueue(request)
        await self.scheduler.claim()
        await self.execute('UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval \'1 second\'')
        require((await self.scheduler.claim())['status']=='empty' and (await t['worker'].inspect(request.id))['state']=='dead','crash-budget')
        self.passed('crash_and_takeover_cannot_bypass_attempt_budget')
        await self.reset_jobs()

    async def wait_locked(self, task):
        for _ in range(60):
            if task.done():
                raise CheckFailure('operation-finished-before-lock-observed')
            if await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' AND query LIKE %s)", ('%'+self.schema+'%jobs_call%',)):
                return
            await asyncio.sleep(0.02)
        raise CheckFailure('operation-lock-not-observed')

    async def wait_expired(self, query, args=None):
        for _ in range(100):
            if await self.scalar(query,args):
                return
            await asyncio.sleep(0.02)
        raise CheckFailure('test-deadline-not-observed')

    async def lock_races(self):
        t=self.tenants[0]
        request=self.request()
        await t['producer'].enqueue(request)
        claim=await self.scheduler.claim()
        await self.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()+interval '800 milliseconds'")
        before=await self.snapshot()
        task=None
        try:
            async with direct_connection(self.direct) as blocker:
                async with blocker.transaction():
                    await blocker.execute(sql.SQL('SELECT id FROM {}.principals WHERE id=%s FOR UPDATE').format(sql.Identifier(self.schema)),(t['actor'],))
                    task=asyncio.create_task(t['worker'].heartbeat(self.lease(claim)))
                    await self.wait_locked(task)
                    await self.wait_expired('SELECT clock_timestamp()>=lease_until FROM {s}.outbox_jobs')
            await self.rejected('heartbeat_rechecks_expiry_after_actor_lock_wait',task,'stale')
        finally:
            if task is not None and not task.done():task.cancel()
            if task is not None:await asyncio.gather(task,return_exceptions=True)
        require(await self.snapshot()==before,'blocked-heartbeat-no-mutation')
        self.passed('blocked_worker_cannot_resurrect_expired_lease')
        await self.reset_jobs()
        await self.execute("UPDATE {s}.job_callers SET expires_at=clock_timestamp()+interval '800 milliseconds' WHERE credential_hash=%s",(t['producer']._credential_hash,))
        task=None
        try:
            async with direct_connection(self.direct) as blocker:
                async with blocker.transaction():
                    await blocker.execute(sql.SQL('SELECT singleton FROM {}.job_scheduler FOR UPDATE').format(sql.Identifier(self.schema)))
                    task=asyncio.create_task(t['producer'].enqueue(self.request()))
                    await self.wait_locked(task)
                    await self.wait_expired('SELECT clock_timestamp()>=expires_at FROM {s}.job_callers WHERE credential_hash=%s',(t['producer']._credential_hash,))
            await self.rejected('producer_expiry_rechecked_after_scheduler_lock',task,'forbidden')
        finally:
            if task is not None and not task.done():task.cancel()
            if task is not None:await asyncio.gather(task,return_exceptions=True)
            await self.execute("UPDATE {s}.job_callers SET expires_at=clock_timestamp()+interval '1 day' WHERE credential_hash=%s",(t['producer']._credential_hash,))
        require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs')==0,'expired-producer-no-enqueue')
        self.passed('expired_credential_waiting_for_lock_creates_no_job')
        request=self.request()
        await t['producer'].enqueue(request)
        claim=await self.scheduler.claim()
        results=await asyncio.gather(t['worker'].complete(self.lease(claim)),t['worker'].complete(self.lease(claim)),return_exceptions=True)
        require(sum(isinstance(r,dict) and r.get('status')=='ok' for r in results)==1 and sum(isinstance(r,JobFailure) and r.status in {'stale','unavailable'} for r in results)==1,'complete-race')
        self.passed('concurrent_completion_has_one_winner')
        await self.reset_jobs()

    async def authorization_uow_race(self):
        a,b=self.tenants[:2]
        await a['producer'].enqueue(self.request())
        await b['producer'].enqueue(self.request(1))
        claim=await self.scheduler.claim()
        require(claim['tenant_id']==str(a['tenant']),'uow-first-tenant')
        other=RuntimeDatabase(self.target,schema=self.schema)
        await other.open()
        worker_task=None
        try:
            repo=PostgresJobRepository(other)
            scheduler=JobService(repo,'x'*43);scheduler._credential_hash=self.scheduler._credential_hash
            worker_b=JobService(repo,b['secret'])
            # Actual AUTH-03 scope holds the session FOR UPDATE, not an approximated lock.
            async with self.repo_authorization.scope(digest('session',a['token'])) as unit:
                worker_task=asyncio.create_task(a['worker'].heartbeat(self.lease(claim)))
                await self.wait_locked(worker_task)
                claimed_b=await scheduler.claim()
                require(claimed_b['tenant_id']==str(b['tenant']),'uow-b-claim')
                await worker_b.heartbeat(self.lease(claimed_b))
                self.passed('blocked_authority_a_does_not_block_b_claim_or_heartbeat')
                bound=JobService(PostgresJobRepository(self.database,connection=unit.connection),'x'*43)
                bound._credential_hash=a['producer']._credential_hash
                queued=await bound.enqueue(self.request())
                require(queued['status']=='ok','bound-auth-enqueue')
            require((await worker_task)['status']=='ok','auth-worker-no-deadlock')
            self.passed('auth03_scope_enqueue_and_worker_follow_same_lock_order_without_deadlock')
            require(await self.scalar('SELECT count(*) FROM {s}.outbox_jobs')==3,'auth-outbox-commit')
            self.passed('real_auth03_scope_commits_bound_outbox')
        finally:
            if worker_task is not None and not worker_task.done():worker_task.cancel()
            if worker_task is not None:await asyncio.gather(worker_task,return_exceptions=True)
            await other.close()
        await self.reset_jobs()

    async def uncommitted_heartbeat_race(self):
        for n in range(5):
            await self.tenants[n]['producer'].enqueue(self.request(n))
        claims=[await self.scheduler.claim() for _ in range(4)]
        a=claims[0];index=next(i for i,t in enumerate(self.tenants) if str(t['tenant'])==a['tenant_id'])
        t=self.tenants[index]
        await t['producer'].enqueue(self.request(index))
        await self.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()+interval '1200 milliseconds' WHERE id=%s",(UUID(a['id']),))
        task=None
        try:
            async with self.database.transaction() as connection:
                worker=JobService(PostgresJobRepository(self.database,connection=connection),t['secret'])
                await worker.heartbeat(self.lease(a))
                # A concurrent connection still sees the old committed expiry until this UoW commits.
                await self.wait_expired('SELECT clock_timestamp()>=lease_until FROM {s}.outbox_jobs WHERE id=%s',(UUID(a['id']),))
                result=await self.scheduler.claim()
                require(result['status']=='empty','renewal-global-overcommit')
            require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE state='running' AND lease_until>clock_timestamp()") == 4,'four-renewed-slots')
            require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE tenant_id=%s AND state='running' AND lease_until>clock_timestamp()",(t['tenant'],)) == 1,'one-renewed-tenant-slot')
            self.passed('uncommitted_heartbeat_cannot_overcommit_global_or_tenant_capacity')
        finally:
            if task is not None and not task.done():task.cancel()
            if task is not None:await asyncio.gather(task,return_exceptions=True)
        await self.reset_jobs()

    async def privilege_drift_checks(self):
        grants=[('table_read','SELECT ON {s}.outbox_jobs'),('caller_write','UPDATE ON {s}.job_callers'),
                ('helper_execute','EXECUTE ON FUNCTION {s}._job_actor_valid({s}.outbox_jobs)')]
        for name,grant in grants:
            await self.execute('GRANT '+grant+' TO {a}')
            try:
                async with self.database.transaction() as connection:
                    cursor=await connection.execute(sql.SQL('SELECT {}.runtime_role_is_valid()').format(sql.Identifier(self.schema)))
                    require((await cursor.fetchone())==(False,), 'drift-guard-'+name)
                await self.rejected('runtime_guard_blocks_accidental_'+name,self.scheduler.claim(),'unavailable')
            finally:
                await self.execute('REVOKE '+grant+' FROM {a}')
        async with self.database.transaction() as connection:
            cursor=await connection.execute(sql.SQL('SELECT {}.runtime_role_is_valid()').format(sql.Identifier(self.schema)))
            require((await cursor.fetchone())==(True,),'drift-restored')
        self.passed('least_privilege_restored_after_drift_probe')

    async def fairness_checks(self):
        a, b = self.tenants[:2]
        for _ in range(100):
            await a['producer'].enqueue(self.request())
        await self.rejected('tenant_a_pending_capacity_bounded', a['producer'].enqueue(self.request()), 'capacity')
        request_b = self.request(1)
        await b['producer'].enqueue(request_b)
        claims = [await self.scheduler.claim(),await self.scheduler.claim()]
        require({c['tenant_id'] for c in claims}=={str(a['tenant']),str(b['tenant'])},'saturated-a-b-claims')
        require((await self.scheduler.claim())['status']=='empty','per-tenant-running-limit')
        self.passed('tenant_a_full_queue_and_running_quota_preserve_b_capacity')
        for c in claims:
            await self.executor(c).complete(self.lease(c))
        await b['producer'].enqueue(self.request(1))
        # Both tenants ready again: round robin prevents repeated A from overtaking B.
        first, second = await self.scheduler.claim(), await self.scheduler.claim()
        require(first['tenant_id']!=second['tenant_id'],'round-robin')
        self.passed('tenant_round_robin_preserves_distinct_turns')
        for c in (first,second):
            await self.executor(c).complete(self.lease(c))
        for _ in range(3):
            await b['producer'].enqueue(self.request(1))
        turns=[]
        for _ in range(4):
            c=await self.scheduler.claim()
            turns.append(c['tenant_id'])
            await self.executor(c).complete(self.lease(c))
        require(turns==[str(a['tenant']),str(b['tenant']),str(a['tenant']),str(b['tenant'])],'sequential-round-robin')
        self.passed('round_robin_prevents_starvation_when_a_completes_immediately')
        await self.reset_jobs()
        for n,t in enumerate(self.tenants):
            await t['producer'].enqueue(self.request(n))
        claims = [await self.scheduler.claim() for _ in range(5)]
        require(sum(c['status']=='ok' for c in claims)==4 and claims[-1]['status']=='empty','global-running-limit')
        await self.executor(claims[0]).complete(self.lease(claims[0]))
        fifth = await self.scheduler.claim()
        require(fifth['status']=='ok' and fifth['tenant_id'] not in {c.get('tenant_id') for c in claims},'fifth-fair')
        self.passed('global_four_slot_limit_admits_waiting_fifth_after_release')
        await self.reset_jobs()
        request_a, request_b = self.request(), self.request(1)
        await a['producer'].enqueue(request_a)
        await b['producer'].enqueue(request_b)
        async with direct_connection(self.direct) as blocker:
            async with blocker.transaction():
                await blocker.execute(sql.SQL('SELECT id FROM {}.outbox_jobs WHERE id=%s FOR UPDATE').format(sql.Identifier(self.schema)),(request_a.id,))
                claimed = await self.scheduler.claim()
                require(claimed['id']==str(request_b.id),'skip-locked-tenant')
        self.passed('row_locked_tenant_a_does_not_block_b_or_spend_a_turn')
        await b['worker'].fail(self.lease(claimed),'rate_limited')
        require((await self.scheduler.claim())['id']==str(request_a.id),'rate-limited-skip')
        self.passed('tenant_in_rate_limit_backoff_does_not_block_other_tenant')
        await self.reset_jobs()
        await a['producer'].enqueue(self.request())
        expired=await self.scheduler.claim()
        await a['producer'].enqueue(self.request())
        pending_b=self.request(1)
        await b['producer'].enqueue(pending_b)
        await self.execute("UPDATE {s}.outbox_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(UUID(expired['id']),))
        async with direct_connection(self.direct) as blocker:
            async with blocker.transaction():
                await blocker.execute(sql.SQL('SELECT id FROM {}.outbox_jobs WHERE id=%s FOR UPDATE').format(sql.Identifier(self.schema)),(UUID(expired['id']),))
                claimed=await self.scheduler.claim()
                require(claimed['id']==str(pending_b.id),'expired-locked-a-preserves-b')
                require(await self.scalar("SELECT count(*) FROM {s}.outbox_jobs WHERE tenant_id=%s AND state='running'",(a['tenant'],))==1,'expired-a-reservation')
        self.passed('expired_locked_running_a_reserves_slot_without_starving_b')
        await self.reset_jobs()

    async def revocation_checks(self):
        t = self.tenants[0]
        changes = {
            'session_logout': ('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE id=%s',(t['session'],), 'UPDATE {s}.sessions SET revoked_at=NULL WHERE id=%s'),
            'principal_disabled': ('UPDATE {s}.principals SET active=false WHERE id=%s',(t['actor'],), 'UPDATE {s}.principals SET active=true WHERE id=%s'),
            'tenant_disabled': ('UPDATE {s}.tenants SET active=false WHERE id=%s',(t['tenant'],), 'UPDATE {s}.tenants SET active=true WHERE id=%s'),
            'membership_disabled': ('UPDATE {s}.memberships SET active=false WHERE id=%s',(t['member'],), 'UPDATE {s}.memberships SET active=true WHERE id=%s'),
            'access_revision_changed': ('UPDATE {s}.memberships SET access_revision=access_revision+1 WHERE id=%s',(t['member'],), 'UPDATE {s}.memberships SET access_revision=access_revision-1 WHERE id=%s'),
            'session_expired': ("WITH e AS(SELECT clock_timestamp()-interval '1 second' t) UPDATE {s}.sessions SET authenticated_at=e.t-interval '8 hours',created_at=e.t-interval '8 hours',last_seen_at=e.t-interval '1 hour',expires_at=e.t FROM e WHERE id=%s",(t['session'],), "WITH e AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET authenticated_at=e.t,created_at=e.t,last_seen_at=e.t,expires_at=e.t+interval '8 hours' FROM e WHERE id=%s"),
            'session_idle': ("WITH e AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET authenticated_at=e.t-interval '2 hours',created_at=e.t-interval '2 hours',last_seen_at=e.t-interval '31 minutes',expires_at=e.t+interval '6 hours' FROM e WHERE id=%s",(t['session'],), "WITH e AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET authenticated_at=e.t,created_at=e.t,last_seen_at=e.t,expires_at=e.t+interval '8 hours' FROM e WHERE id=%s"),
        }
        for name,(query,args,restore) in changes.items():
            request = self.request()
            await t['producer'].enqueue(request)
            await self.execute(query,args)
            claim = await self.scheduler.claim()
            await self.rejected('queued_'+name+'_holds_before_reference',t['worker'].reference(self.lease(claim)),'held')
            require((await t['worker'].inspect(request.id))['state']=='held','held-'+name)
            await self.execute(restore,args)
            # No automatic resurrection after restoring the actor; old identity stays held.
            require((await self.scheduler.claim())['status']=='empty','no-auto-rebind-'+name)
            await self.reset_jobs()
        request = self.request()
        await t['producer'].enqueue(request)
        claim = await self.scheduler.claim()
        await t['worker'].reference(self.lease(claim))
        await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE id=%s',(t['session'],))
        await self.rejected('revocation_after_reference_blocks_completion',t['worker'].complete(self.lease(claim)),'held')
        await self.execute('UPDATE {s}.sessions SET revoked_at=NULL WHERE id=%s',(t['session'],))
        await self.reset_jobs()


async def verify(direct,base):
    schema='db03_probe_'+uuid4().hex
    database=None;owned=False;stage='namespace'
    try:
        before=await migrate(direct)
        async with direct_connection(direct) as connection:
            require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'namespace')
        owned=True;stage='migrate';await migrate(direct,schema=schema,upgrade=True)
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1])
        database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            h=Harness(connection,database,schema,direct,target)
            stage='setup';await h.setup_jobs()
            for name in ('boundary_checks','outbox_checks','claim_checks','retry_checks','fairness_checks','revocation_checks','lock_races','authorization_uow_race','uncommitted_heartbeat_race','privilege_drift_checks'):
                stage=name;await getattr(h,name)()
                print('Job checks completed: '+name,flush=True)
        require(await migrate(direct)==before,'platform-history-unchanged')
        return dict(passed=True,check_count=len(h.checks),checks=h.checks,platform_revision=before[0],
                    tested_revision=7,platform_schema_modified=False,real_postgres_restricted_runtime=True,disposable_cleanup='completed')
    except Exception as error:
        raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--output',type=Path,default=Path('local/job01/jobs-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live is required.')
    safe_transport_logs()
    try:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text('{"passed":false,"status":"running"}',encoding='utf-8')
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
        verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps(result,sort_keys=True))
    except Exception as error:
        print('Job verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_operation_failure'),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':main()
