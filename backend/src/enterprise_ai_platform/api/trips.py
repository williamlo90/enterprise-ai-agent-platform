from urllib.parse import parse_qsl
from fastapi import APIRouter,Request
from enterprise_ai_platform.api.identity import cookie,SESSION_COOKIE
from enterprise_ai_platform.modules.trips.ports import TripFailure

router=APIRouter()


@router.get('/api/v1/trips')
async def listing(request:Request):
    request.state.authorization_required=True
    service=request.app.state.services.trips
    if service is None:raise TripFailure(503)
    if request.headers.get('content-length','0')!='0' or request.headers.get('transfer-encoding'):raise TripFailure(400)
    try:
        pairs=parse_qsl(request.scope.get('query_string',b'').decode('utf-8'),strict_parsing=True,keep_blank_values=True,max_num_fields=2,errors='strict')
        values=dict(pairs)
        if len(values)!=len(pairs) or set(values)-{'after','limit'} or any(not v for v in values.values()):raise ValueError
        limit=values.get('limit','20')
        if limit not in tuple(str(n) for n in range(1,51)):raise ValueError
    except (ValueError,UnicodeError):raise TripFailure(400) from None
    return await service.listing(cookie(request,SESSION_COOKIE),after=values.get('after'),limit=int(limit))
