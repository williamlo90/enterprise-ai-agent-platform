import asyncio
from dataclasses import replace
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.jobs.domain import JobRequest, Lease
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.modules.jobs.ports import JobFailure
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository


def request():
    return JobRequest(uuid4(),'agent.resume',uuid4(),uuid4(),1,uuid4(),uuid4(),uuid4(),1)


@pytest.mark.parametrize('field,value', [('id','not-an-id'),('actor_id',None),('session_id','x'),
    ('membership_id',True),('business_key',1),('target_id','x'),('job_type','untrusted.tool'),
    ('expected_version',True),('expected_version',0),('expected_version',10**18),('expected_version',1.0),
    ('access_revision',False),('retry_limit',0),('retry_limit',6),('retry_limit',True)])
def test_invalid_request_fails_before_persistence(field,value):
    with pytest.raises(ValueError):
        replace(request(),**{field:value})


@pytest.mark.parametrize('value',[True,0,-1,1.5,10**18])
def test_lease_requires_bounded_positive_generation(value):
    with pytest.raises(ValueError):
        Lease(uuid4(),uuid4(),value)


class Repo:
    def __init__(self,result=None):
        self.calls=[]
        self.result=result if result is not None else {'status':'ok'}
    async def call(self,operation,payload):
        self.calls.append((operation,payload))
        return self.result


def test_only_domain_hash_crosses_persistence_boundary():
    repo=Repo();secret='a'*43
    service=JobService(repo,secret)
    asyncio.run(service.enqueue(request()))
    body=repo.calls[0][1]
    assert secret not in str(body) and len(body['credential_hash'])==64
    assert set(body)=={'credential_hash','id','job_type','business_key','target_id','expected_version','actor_id','membership_id','session_id','access_revision','retry_limit'}
    assert 'tenant_id' not in body and 'payload' not in body


@pytest.mark.parametrize('value',['','a'*42,'a'*44,'!'*43,None,123])
def test_malformed_job_credential_rejected(value):
    with pytest.raises(ValueError):JobService(Repo(),value)


@pytest.mark.parametrize('status',['stale','held','invalid','forbidden','unauthenticated','capacity','conflict','missing','unrecognized'])
def test_persistence_denial_is_safe_and_never_retried(status):
    repo=Repo({'status':status,'debug':'sensitive-text'})
    with pytest.raises(JobFailure) as error:
        asyncio.run(JobService(repo,'a'*43).claim())
    assert error.value.status==(status if status!='unrecognized' else 'unavailable')
    assert 'sensitive' not in str(error.value) and len(repo.calls)==1


def test_no_arbitrary_failure_or_stale_lease_type_reaches_database():
    repo=Repo();service=JobService(repo,'a'*43)
    with pytest.raises(ValueError):asyncio.run(service.fail(Lease(uuid4(),uuid4(),1),'raw provider message'))
    with pytest.raises(ValueError):asyncio.run(service.complete({'id':str(uuid4()),'generation':1}))
    assert repo.calls==[]


def test_cancellation_is_not_masked_as_retryable_failure():
    class Connection:
        async def execute(self,*args):raise asyncio.CancelledError()
    class Database:schema='platform'
    adapter=PostgresJobRepository(Database(),connection=Connection())
    with pytest.raises(asyncio.CancelledError):asyncio.run(adapter.call('enqueue',{}))


def test_bound_uow_rejected_enqueue_propagates_to_rollback():
    class Cursor:
        async def fetchone(self):return ({'status':'capacity'},)
    class Connection:
        async def execute(self,*args):return Cursor()
    class Database:schema='platform'
    with pytest.raises(JobFailure) as error:
        asyncio.run(PostgresJobRepository(Database(),connection=Connection()).call('enqueue',{}))
    assert error.value.status=='capacity'


def test_driver_failure_does_not_expose_query_or_credentials():
    class Connection:
        async def execute(self,*args):raise RuntimeError('private-dsn-and-sql')
    class Database:schema='platform'
    with pytest.raises(JobFailure) as error:
        asyncio.run(PostgresJobRepository(Database(),connection=Connection()).call('enqueue',{}))
    assert str(error.value)=='Job operation rejected.' and error.value.__cause__ is None
