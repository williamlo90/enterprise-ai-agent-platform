import asyncio
import copy
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from reimbursement_service.api.draft_sources import router
from reimbursement_service.modules.access.ports import VerifiedActor,AccessFailure,request_hash
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest
from reimbursement_service.modules.trips.service import TripsService
from reimbursement_service.config import Settings,ConfigurationError


def uid():return str(uuid4())


async def probe(kind='information',change=lambda body,headers,state:None):
    actor,tenant,member,trip,claim=uid(),uid(),uid(),uid(),uid();calls=[]
    def payload(action,obj):return dict(schema_version=1,action=action,tenant_id=tenant,actor_id=actor,operation_id=uid(),object=dict(kind='trip' if action=='trip.read' else 'claim',id=obj,version=1),data={})
    trip_payload=payload('trip.read',trip)
    body=dict(payload=payload('claim.read',claim) if kind=='information' else trip_payload,trip_id=trip,trip_payload=trip_payload if kind=='information' else None)
    state=dict(owner=actor,status='needs_information',trip_member=member,changed=False)
    headers={'authorization':'Bearer '+'d'*43,'x-eai-delegation':'claim-token' if kind=='information' else 'trip-token','x-eai-trip-delegation':'trip-token','content-type':'application/json'}
    class Verifier:
        async def verify(self,token,payload):
            expected='claim.read' if token=='claim-token' else 'trip.read'
            assert payload['action']==expected
            return VerifiedActor(payload['actor_id'],tenant,member if expected=='claim.read' else state['trip_member'],1,expected,payload['operation_id'],request_hash(payload),100,150,False)
    trip_dispatch=AccessService(Verifier(),Dispatcher('platform-dispatcher',credential_digest('d'*43),frozenset({'trip.read'})),clock=lambda:100)
    claim_dispatch=AccessService(Verifier(),Dispatcher('platform-dispatcher',credential_digest('d'*43),frozenset({'claim.read'})),clock=lambda:100)
    class Claims:
        count=0
        async def read(self,payload,authority):
            self.count+=1
            return dict(id=claim,owner_id=state['owner'],trip_id=trip,state=state['status'],version=2+(1 if state['changed'] and self.count>1 else 0),revision=1,policy=dict(id=uid(),version=1,digest='a'*64)) if self.count==1 else {**self.first,'version':2+(1 if state['changed'] else 0)}
    claims=Claims();original=claims.read
    async def read(payload,authority):
        value=await original(payload,authority)
        if claims.count==1:claims.first=copy.deepcopy(value)
        return value
    claims.read=read
    class Trips:
        async def detail(self,trip_id,authority):calls.append(authority);return dict(id=trip_id)
    trips=TripsService(Trips(),None,clock=lambda:100)
    app=FastAPI();app.include_router(router(trips,claims,trip_dispatch,claim_dispatch))
    @app.exception_handler(AccessFailure)
    async def fail(request:Request,error):return JSONResponse({'error':'unavailable'},status_code=error.status)
    change(body,headers,state)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://source.test') as client:
        result=await client.post('/service/v1/draft-sources',json=body,headers=headers)
    return result,calls


@pytest.mark.parametrize('kind',['submission','information'])
def test_source_route_uses_separately_verified_trip_read_authority(kind):
    response,calls=asyncio.run(probe(kind))
    assert response.status_code==200 and len(calls)==1 and calls[0].action=='trip.read'


@pytest.mark.parametrize('change,status',[
    (lambda b,h,s:h.pop('x-eai-trip-delegation'),400),
    (lambda b,h,s:s.update(trip_member=uid()),403),
    (lambda b,h,s:s.update(owner=uid()),403),
    (lambda b,h,s:s.update(status='pending_approval'),409),
])
def test_information_source_rejects_missing_trip_authority_other_identity_and_non_owner(change,status):
    response,calls=asyncio.run(probe(change=change))
    assert response.status_code==status and not calls


def test_claim_version_change_during_trip_read_discards_source_snapshot():
    response,calls=asyncio.run(probe(change=lambda b,h,s:s.update(changed=True)))
    assert response.status_code==409 and len(calls)==1


def test_source_feature_requires_both_existing_read_features_and_strict_boolean():
    assert not Settings(database_url='fixture').draft_sources_enabled
    for settings in (dict(draft_sources_enabled=True),dict(draft_sources_enabled='true')):
        with pytest.raises(ConfigurationError):Settings(database_url='fixture',**settings)
