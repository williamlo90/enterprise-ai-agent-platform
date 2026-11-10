from types import SimpleNamespace
import json
import asyncio
from uuid import uuid4

import pytest

from enterprise_ai_platform.agentcore_execution import claim_value
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.integrations.agentcore.clients import Gateway, ExecutionMemory


def binding():
    return {'id': str(uuid4()), 'tenant_id': str(uuid4()), 'worker_id': str(uuid4()), 'generation': 1}


def test_browser_cannot_supply_actor_or_execution_epoch():
    original = binding()
    assert claim_value(original) == original
    for injected in ({'actor_id': str(uuid4())}, {'epoch': 1}, {'credential_hash': 'a' * 64}):
        with pytest.raises(ValueError):
            claim_value({**original, **injected})


@pytest.mark.parametrize('generation', [True, 0, -1, '1', 10**18])
def test_forged_lease_generation_is_rejected(generation):
    with pytest.raises(ValueError):
        claim_value({**binding(), 'generation': generation})


def test_financial_action_is_denied_before_network_access():
    gateway = Gateway.__new__(Gateway)
    for tool in ('confirm', 'approve', 'pay', 'dispatch', 'claim.submit'):
        with pytest.raises(ValueError):
            gateway.call(tool, binding(), {})


def test_memory_uses_verified_tenant_actor_and_original_session(monkeypatch):
    requests = []
    client = SimpleNamespace(create_event=lambda **kw: requests.append(kw), close=lambda: None)
    monkeypatch.setattr('enterprise_ai_platform.integrations.agentcore.clients.boto3.client', lambda *a, **kw: client)
    actor, session, run = str(uuid4()), str(uuid4()), str(uuid4())
    source = {'actor_id': actor, 'session_id': session, 'target_id': run}
    first = ExecutionMemory('memory', 'ap-southeast-1', 'tenant-a', source)
    other_tenant = ExecutionMemory('memory', 'ap-southeast-1', 'tenant-b', source)
    other_user = ExecutionMemory('memory', 'ap-southeast-1', 'tenant-a', {**source, 'actor_id': str(uuid4())})
    assert len({first.actor, other_tenant.actor, other_user.actor}) == 3
    assert len({first.session, other_tenant.session, other_user.session}) == 3
    first.record('waiting')
    assert json.loads(requests[0]['payload'][0]['blob']) == {'run_id': run, 'phase': 'waiting'}
    assert actor not in str(requests) and session not in str(requests)


def test_memory_reads_the_aws_blob_string_wire_format(monkeypatch):
    run=str(uuid4())
    client=SimpleNamespace(list_events=lambda **kwargs:{'events':[{'payload':[
        {'blob':json.dumps({'run_id':run,'phase':'completed'})},
        {'blob':'{phase=completed, run_id=unreadable}'},
        {'blob':json.dumps({'run_id':run,'phase':'completed','extra':'ignored'})}]}]},close=lambda:None)
    monkeypatch.setattr('enterprise_ai_platform.integrations.agentcore.clients.boto3.client',lambda *a,**kw:client)
    memory=ExecutionMemory('memory','ap-southeast-1','tenant-a',{'actor_id':str(uuid4()),'session_id':str(uuid4()),'target_id':run})
    assert memory.recent()==[{'run_id':run,'phase':'completed'}]


def test_runtime_schema_is_operator_scoped():
    accepted = Settings(env='test', host='127.0.0.1', port=8000, database_schema='db03_probe_' + 'a' * 32)
    assert accepted.database_schema.startswith('db03_probe_')
    for schema in ('public', '../platform', 'platform;DROP SCHEMA public', 'another_tenant'):
        with pytest.raises(ValueError):
            Settings(env='test', host='127.0.0.1', port=8000, database_schema=schema)


def test_internal_file_authority_can_exist_without_exposing_upload_routes():
    from enterprise_ai_platform.main import create_app
    from enterprise_ai_platform.composition import ApplicationServices
    files=object()
    services=ApplicationServices(health=None,files=files)
    app=create_app(Settings(env='test',host='127.0.0.1',port=8000,files_upload_enabled=False),services=services)
    assert app.state.services.files is files
    assert not any(path.startswith('/api/v1/files') for path in app.openapi()['paths'])
    upload_app=create_app(Settings(env='test',host='127.0.0.1',port=8000),services=services)
    assert '/api/v1/files' in upload_app.openapi()['paths']


def test_rpc_retries_only_explicit_rolled_back_lock_rejections(monkeypatch):
    from enterprise_ai_platform.infrastructure.database.connection import rpc_result
    async def pause(_):pass
    monkeypatch.setattr('enterprise_ai_platform.infrastructure.database.connection.asyncio.sleep',pause)
    class Connection:
        def __init__(self,values):self.values=list(values);self.calls=0
        async def execute(self,*args):
            self.calls+=1;value=self.values.pop(0)
            if isinstance(value,Exception):raise value
            async def fetchone():return (value,)
            return SimpleNamespace(fetchone=fetchone)
    async def check():
        busy=Connection([{'status':'unavailable'},{'status':'ok'}])
        assert (await rpc_result(busy,'query',()))['status']=='ok' and busy.calls==2
        denied=Connection([{'status':'forbidden'}])
        assert (await rpc_result(denied,'query',()))['status']=='forbidden' and denied.calls==1
        unknown=Connection([OSError('Connection lost')])
        with pytest.raises(OSError):await rpc_result(unknown,'query',())
        assert unknown.calls==1
    asyncio.run(check())


def test_terminal_heartbeat_preserves_committed_runtime_result_without_reinvoking(monkeypatch):
    import io,time
    from enterprise_ai_platform.integrations.agentcore.clients import invoke_runtime
    claim=binding();calls=[]
    def invoke(**kwargs):
        calls.append(kwargs);time.sleep(0.03)
        return {'response':io.BytesIO(json.dumps({'status':'ok','job_id':claim['id'],'generation':1,'state':'completed'}).encode())}
    client=SimpleNamespace(invoke_agent_runtime=invoke,stop_runtime_session=lambda **kw:None,close=lambda:None)
    monkeypatch.setattr('enterprise_ai_platform.integrations.agentcore.clients.boto3.client',lambda *a,**kw:client)
    real_sleep=asyncio.sleep
    async def fast_pulse(_):await real_sleep(0.001)
    monkeypatch.setattr('enterprise_ai_platform.integrations.agentcore.clients.asyncio.sleep',fast_pulse)
    class Jobs:
        async def reference(self,lease):return {'job_type':'agent.resume','target_id':str(uuid4())}
        async def heartbeat(self,lease):raise ValueError('Job already completed')
    result=asyncio.run(invoke_runtime('runtime','ap-southeast-1',claim,Jobs(),SimpleNamespace(generation=1)))
    assert result['state']=='completed' and len(calls)==1
