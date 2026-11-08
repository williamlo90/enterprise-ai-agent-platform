"""Service-only receipt routes. Public browser composition never mounts these."""
import json
from fastapi import APIRouter,Request
from enterprise_ai_platform.modules.receipts.ports import ReceiptFailure

router=APIRouter()


async def inputs(request):
    request.state.authorization_required=True
    if request.scope.get('query_string'):raise ReceiptFailure(400)
    headers={}
    for k,v in request.scope.get('headers',[]):
        k=k.lower()
        if k in {b'authorization',b'x-eai-delegation',b'content-type',b'cookie'}:
            if k in headers:raise ReceiptFailure(401)
            headers[k]=v.decode('latin-1')
    if b'cookie' in headers:raise ReceiptFailure(401)
    authorization=headers.get(b'authorization','')
    if not authorization.startswith('Bearer '):raise ReceiptFailure(401)
    if headers.get(b'content-type')!='application/json':raise ReceiptFailure(400)
    raw=await request.body()
    if len(raw)>131072:raise ReceiptFailure(400)
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError
            result[k]=v
        return result
    try:
        body=json.loads(raw.decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if type(body) is not dict:raise ValueError
    except (ValueError,UnicodeError):raise ReceiptFailure(400) from None
    return authorization[7:],headers.get(b'x-eai-delegation'),body


@router.post('/internal/v1/receipt-pins')
async def prepare(request:Request):
    credential,delegation,body=await inputs(request)
    result=await request.app.state.services.receipts.prepare(credential,delegation,body)
    request.state.receipt_authority_deadline=result['expires_at']
    return result


@router.post('/internal/v1/receipt-pins/{operation_id}/finalise')
async def finalise(operation_id:str,request:Request):
    credential,delegation,body=await inputs(request)
    if delegation is not None:raise ReceiptFailure(400)
    result=await request.app.state.services.receipts.finalise(credential,operation_id,body)
    request.state.receipt_authority_deadline=result['expires_at']
    return result
