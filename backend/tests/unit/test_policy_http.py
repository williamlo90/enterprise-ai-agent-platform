import asyncio
import json
import httpx
import pytest
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.api.browser_context import mutation_target

PID='00000000-0000-0000-0000-000000000002'
KEY='00000000-0000-0000-0000-000000000006'
PUBLIC=f'/api/v1/policy-packages/{PID}/versions/1/prepare'
PRIVATE=f'/internal/v1/policy-packages/{PID}/eligibility'

class Policy:
    def __init__(self):self.calls=[]
    async def mutate(self,*args):self.calls.append(args);return {'state':'prepared'}
    async def eligibility(self,*args):self.calls.append(args);return {'allow':False}

def app_for(policy,internal=False):
    return (create_internal_app if internal else create_app)(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=None,policies=policy))

def request(app,path,body,headers=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://policy.example.test') as client:
            return await client.post(path,content=body,headers=headers or {'content-type':'application/json'})
    return asyncio.run(run())

def test_public_and_private_policy_surfaces_are_separate_and_opt_in():
    policy=Policy();payload=json.dumps({'key':KEY,'expected_version':1})
    assert request(app_for(policy),PUBLIC,payload).status_code==200
    assert request(app_for(policy),PRIVATE,'{}').status_code==404
    assert request(app_for(policy,True),PUBLIC,payload).status_code==404
    assert request(app_for(None),PUBLIC,payload).status_code==404
    assert mutation_target('POST',PUBLIC)==('policies','prepare')
    assert mutation_target('POST',PUBLIC.replace('/prepare','/dispatch'))==('policies','ack')

@pytest.mark.parametrize('raw,path',[
    ('{"key":"a","key":"b"}',PUBLIC),
    ('{"x":NaN}',PUBLIC),
    ('[]',PUBLIC),
    (json.dumps({'package_id':PID}),PUBLIC),
    ('{}',PUBLIC+'?tenant_id=foreign'),
    ('{}',PUBLIC.replace('/1/','/01/')),
])
def test_ambiguous_or_identity_overriding_inputs_do_not_reach_service(raw,path):
    policy=Policy();response=request(app_for(policy),path,raw)
    assert response.status_code==400 and policy.calls==[]
    assert response.headers['cache-control']=='no-store'

@pytest.mark.parametrize('kind',['cookie','duplicate_bearer','path_policy_mismatch','query','duplicate_json','missing_bearer'])
def test_private_transport_rejects_untrusted_or_mismatched_requests(kind):
    policy=Policy();headers=[('content-type','application/json'),('authorization','Bearer '+'r'*43),('x-eai-delegation','controlled')]
    body=json.dumps({'policy':{'id':PID}});path=PRIVATE
    if kind=='cookie':headers.append(('cookie','__Host-eai-session=untrusted'))
    elif kind=='duplicate_bearer':headers.append(('authorization','Bearer '+'r'*43))
    elif kind=='path_policy_mismatch':body=json.dumps({'policy':{'id':KEY}})
    elif kind=='query':path+='?role=approver'
    elif kind=='duplicate_json':body='{"policy":{},"policy":{}}'
    else:headers=[h for h in headers if h[0]!='authorization']
    assert request(app_for(policy,True),path,body,headers).status_code in (400,401)
    assert policy.calls==[]
