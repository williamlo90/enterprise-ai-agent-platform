"""Composition for authenticated durable jobs shared by Runtime and Gateway Lambda."""
import json
import os
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.modules.jobs.ports import Lease
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository


def claim_value(value):
    if type(value) is not dict or set(value) != {'id', 'tenant_id', 'generation', 'worker_id'}:
        raise ValueError('Invalid job binding.')
    for key in ('id', 'tenant_id', 'worker_id'):
        if type(value[key]) is not str or str(UUID(value[key])) != value[key] or UUID(value[key]).int == 0:
            raise ValueError('Invalid job binding.')
    if type(value['generation']) is not int or not 1 <= value['generation'] < 10**18:
        raise ValueError('Invalid job generation.')
    return dict(value)


async def bound_job(resources, value):
    claim = claim_value(value)
    registry = json.loads(os.environ['EXECUTORS_JSON'])
    credential = registry.get(claim['worker_id'])
    if not isinstance(credential, str):
        raise ValueError('Executor not configured.')
    jobs = JobService(PostgresJobRepository(resources.database), credential)
    lease = Lease(UUID(claim['id']), UUID(claim['tenant_id']), claim['generation'])
    ref = await jobs.reference(lease)
    if ref.get('job_type') != 'agent.resume' or ref.get('authority_scope') != 'actor_only':
        raise ValueError('Agent lease required.')
    for field in ('target_id', 'actor_id', 'session_id'):
        if type(ref.get(field)) is not str or str(UUID(ref[field])) != ref[field] or UUID(ref[field]).int == 0:
            raise ValueError('Original actor/session required.')
    async with resources.database.transaction() as connection:
        route = (await (await connection.execute(
            sql.SQL('SELECT {}.worker_route(%s)').format(sql.Identifier(resources.database.schema)),
            (Jsonb({'credential_hash': jobs._credential_hash, 'id': claim['id'], 'generation': claim['generation']}),)
        )).fetchone())[0]
    if route.get('status') != 'ok' or route.get('target_id') != ref['target_id'] or route.get('agent_kind') not in {'rag', 'assisted'}:
        raise ValueError('Agent route unavailable.')
    binding = {'credential_hash': jobs._credential_hash, 'tenant_id': claim['tenant_id'],
               'run_id': ref['target_id'], 'job_id': claim['id'], 'generation': claim['generation']}
    return claim, jobs, lease, ref, route, binding
