"""Optional real receiver routes with explicit list/publisher authority gates."""
import base64
from fastapi import APIRouter,Request
from reimbursement_service.api.errors import error_response
from reimbursement_service.api.operations import strict_json
from reimbursement_service.modules.access.service import AccessService
from reimbursement_service.modules.access.ports import AccessFailure,FailureKind
from reimbursement_service.modules.trips.service import TripsService
from reimbursement_service.modules.policies.service import PackagesService


def header(request,name):
    values=[v for k,v in request.scope['headers'] if k.lower()==name]
    if len(values)!=1:raise ValueError
    return values[0].decode('ascii')


def scope_headers(request):
    counts={}
    for key,value in request.scope['headers']:
        key=key.lower()
        if key in {b'authorization',b'x-eai-delegation',b'content-type',b'idempotency-key',b'x-eai-operation-id'}:
            counts[key]=counts.get(key,0)+1
    if any(count>1 for count in counts.values()) or any(k.lower()==b'cookie' for k,v in request.scope['headers']):
        raise AccessFailure(FailureKind.UNAUTHENTICATED)


def router(trips:TripsService,packages:PackagesService,dispatch:AccessService):
    routes=APIRouter()

    @routes.get('/service/v1/trips')
    async def listing(request:Request):
        scope_headers(request)
        try:
            pairs=list(request.query_params.multi_items())
            if len(dict(pairs))!=len(pairs) or set(dict(pairs))-{'after','limit'}:raise ValueError
            values=dict(pairs);raw_limit=values.get('limit','20')
            if not raw_limit.isascii() or not raw_limit.isdecimal() or str(int(raw_limit))!=raw_limit:raise ValueError
            operation_id=header(request,b'x-eai-operation-id')
        except (ValueError,UnicodeError):return error_response(400,request.state.request_id)
        result,deadline=await trips.listing(request.scope['headers'],operation_id,values.get('after'),int(raw_limit))
        request.scope['state']['operation_authority_deadline']=deadline
        return result

    @routes.get('/service/v1/trips/{trip_id}')
    async def detail(trip_id:str,request:Request):
        if request.scope.get('query_string'):return error_response(400,request.state.request_id)
        try:
            value=header(request,b'x-eai-trip-payload')
            if len(value)>2048 or '=' in value:raise ValueError
            raw=base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)
            if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=value:raise ValueError
            payload=strict_json(raw)
        except (ValueError,UnicodeError):return error_response(400,request.state.request_id)
        actor=await dispatch.authorize(request.scope['headers'],payload,action='trip.read',object_kind='trip',object_id=trip_id,object_version=1)
        result,deadline=await trips.detail(trip_id,actor,payload)
        request.scope['state']['operation_authority_deadline']=deadline
        return result

    @routes.put('/service/v1/policy-packages/{package_id}/versions/{version}')
    async def receive(package_id:str,version:str,request:Request):
        scope_headers(request)
        if request.scope.get('query_string'):return error_response(400,request.state.request_id)
        try:
            if header(request,b'content-type')!='application/json':raise ValueError
            if not version.isascii() or not version.isdecimal() or str(int(version))!=version:raise ValueError
            key=header(request,b'idempotency-key');operation=header(request,b'x-eai-operation-id')
            body=strict_json(await request.body())
        except (ValueError,TypeError,UnicodeError):return error_response(400,request.state.request_id)
        result,deadline=await packages.receive(request.scope['headers'],package_id,int(version),key,operation,body)
        request.scope['state']['operation_authority_deadline']=deadline
        return result

    return routes
