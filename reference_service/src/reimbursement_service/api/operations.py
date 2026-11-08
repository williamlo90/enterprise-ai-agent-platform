"""Optional current-owner lookup and separately authenticated recovery surface."""
import base64
import json
from fastapi import APIRouter,Request
from reimbursement_service.api.errors import error_response
from reimbursement_service.modules.operations.service import OperationsService
from reimbursement_service.modules.operations.ports import Identity,OperationFailure,uuid
from reimbursement_service.modules.access.service import AccessService


def strict_json(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError
            value[key]=item
        return value
    return json.loads(raw.decode('utf-8'),object_pairs_hook=unique,
                      parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def router(operations:OperationsService,dispatch:AccessService,recovery:AccessService):
    routes=APIRouter()

    async def authorized(request,payload,operation_id,access):
        if request.scope.get('query_string'):raise OperationFailure(400)
        actor=await access.authorize(request.scope['headers'],payload,action='operation.read',
                                     object_kind='operation',object_id=operation_id,object_version=1)
        request.scope['state']['operation_authority_deadline']=actor.expires_at
        return actor

    @routes.get('/service/v1/operations/{operation_id}')
    async def lookup(operation_id:str,request:Request):
        try:
            uuid(operation_id)
            headers=[v for k,v in request.scope['headers'] if k.lower()==b'x-eai-operation-payload']
            if len(headers)!=1 or len(headers[0])>2048:raise ValueError
            value=headers[0].decode('ascii')
            if '=' in value:raise ValueError
            raw=base64.b64decode(value+'='*((-len(value))%4),altchars=b'-_',validate=True)
            if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=value:raise ValueError
            payload=strict_json(raw)
        except (ValueError,UnicodeError):return error_response(400,request.state.request_id)
        actor=await authorized(request,payload,operation_id,dispatch)
        value=await operations.lookup(operation_id,actor,payload)
        request.scope['state']['operation_authority_deadline']=min(actor.expires_at,value['proof']['payload']['expires_at'])
        return value

    @routes.post('/service/v1/operations/{operation_id}/seal-if-absent')
    async def seal(operation_id:str,request:Request):
        try:
            uuid(operation_id);body=strict_json(await request.body())
            if type(body) is not dict or set(body)!={'payload','identity'}:raise ValueError
            identity=Identity(**body['identity'])
            if identity.operation_id!=operation_id:raise ValueError
        except (ValueError,TypeError,UnicodeError):return error_response(400,request.state.request_id)
        actor=await authorized(request,body['payload'],operation_id,recovery)
        value=await operations.seal(identity,actor,body['payload'],recovery_authorized=True)
        request.scope['state']['operation_authority_deadline']=min(actor.expires_at,value['proof']['payload']['expires_at'])
        return value

    return routes
