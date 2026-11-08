import json
from fastapi import APIRouter,Request
from starlette.responses import JSONResponse
from enterprise_ai_platform.api.errors import error_response
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure

router=APIRouter()


@router.post("/internal/v1/actors/verify")
async def verify_actor(request:Request):
    request.state.authorization_required=True
    try:
        headers={}
        for key,value in request.scope.get("headers",[]):
            key=key.lower()
            if key in {b"authorization",b"x-eai-delegation",b"content-type"}:
                if key in headers:raise DelegationFailure(401)
                headers[key]=value.decode("latin-1")
        authorization=headers.get(b"authorization","")
        if not authorization.startswith("Bearer "):raise DelegationFailure(401)
        if headers.get(b"content-type")!="application/json":raise DelegationFailure(400)
        if request.scope.get("query_string"):raise DelegationFailure(400)
        raw=await request.body()
        if len(raw)>131072:raise DelegationFailure(400)
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError
                result[key]=value
            return result
        try:
            body=json.loads(raw.decode("utf-8"),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if type(body) is not dict or set(body)!={"payload"}:raise ValueError
        except (ValueError,UnicodeError):raise DelegationFailure(400) from None
        result=await request.app.state.services.delegation.verify(authorization[7:],headers.get(b"x-eai-delegation"),body["payload"])
        return JSONResponse(result)
    except DelegationFailure as error:
        return error_response(error.status,request.state.request_id)
