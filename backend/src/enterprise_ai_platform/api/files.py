from urllib.parse import parse_qsl
from fastapi import APIRouter,Request
from starlette.responses import JSONResponse,Response
from enterprise_ai_platform.api.identity import cookie,browser_headers,SESSION_COOKIE
from enterprise_ai_platform.modules.files.ports import Upload,FileFailure

router=APIRouter()


def service(request):
    request.state.authorization_required=True
    value=request.app.state.services.files
    if value is None:raise FileFailure(503)
    return value


def header(request,name):
    values=[v.decode('latin1') for k,v in request.scope['headers'] if k.lower()==name.encode()]
    if len(values)!=1:raise FileFailure(400)
    return values[0]


@router.post('/api/v1/files')
async def upload(request:Request):
    instance=service(request)
    try:
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('ascii'),max_num_fields=2,strict_parsing=True,keep_blank_values=True)
        values=dict(pairs)
        if len(values)!=len(pairs) or not {'purpose'}<=set(values) or set(values)-{'purpose','collection_id'} or any(not value for value in values.values()):raise ValueError()
    except (ValueError,UnicodeError):raise FileFailure(400) from None
    value=Upload(values['purpose'],header(request,'content-type'),await request.body(),header(request,'idempotency-key'),values.get('collection_id'))
    result=await instance.upload(cookie(request,SESSION_COOKIE),browser_headers(request),value)
    return JSONResponse(result,status_code=202)


@router.get('/api/v1/files/{file_id}/status')
async def status(file_id:str,request:Request):
    if request.scope.get('query_string'):raise FileFailure(400)
    return JSONResponse(await service(request).status(cookie(request,SESSION_COOKIE),file_id))


@router.get('/api/v1/files/{file_id}/content')
async def content(file_id:str,request:Request):
    if request.scope.get('query_string') or any(k.lower() in {b'range',b'if-none-match',b'if-modified-since'} for k,v in request.scope['headers']):raise FileFailure(400)
    row,data=await service(request).content(cookie(request,SESSION_COOKIE),file_id)
    return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="'+row['id']+'"','X-Content-Type-Options':'nosniff'})


@router.get('/api/v1/files/{file_id}/claims/{claim_id}/content')
async def claim_content(file_id:str,claim_id:str,request:Request):
    if request.scope.get('query_string') or any(k.lower() in {b'range',b'if-none-match',b'if-modified-since'} for k,v in request.scope['headers']):raise FileFailure(400)
    row,data=await service(request).claim_content(cookie(request,SESSION_COOKIE),file_id,claim_id)
    request.state.receipt_authority_deadline=row.pop('_receipt_authority_expires_at')
    return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="'+row['id']+'"','X-Content-Type-Options':'nosniff'})
