from enterprise_ai_platform.integrations.security.tls import internal_tls
"""Exact scoped trips.list catalog grant; independent REF-04 summary validation."""
import asyncio
import base64
import json
import logging
import time
from uuid import uuid4
import httpx
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget,TransportLog
from enterprise_ai_platform.modules.delegation.domain import canonical_catalog,catalog_hash
from enterprise_ai_platform.modules.trips.domain import TripFailure,filters,listing_digest,page


class TripCatalogReader:
    def __init__(self,target:PolicyTarget,signer,*,transport=None,clock=time.time):
        self.target,self.signer,self.clock=target,signer,clock
        for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2'):
            logger=logging.getLogger(name)
            if not any(isinstance(f,TransportLog) for f in logger.filters):logger.addFilter(TransportLog())
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(4,connect=2),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))

    async def close(self):await self.http.aclose()

    async def listing(self,context,after,limit):
        try:
            query=filters(after,limit);operation=str(uuid4());now=int(self.clock());expiry=min(now+60,int(context['authority_expires_at']))
            if self.clock()>=expiry:raise ValueError
            payload=dict(schema_version=2,action='trips.list',tenant_id=context['tenant_id'],actor_id=context['actor_id'],operation_id=operation,
                object=dict(kind='trip_collection',id=None,version=1),data={**query,'request_digest':listing_digest(after,limit)})
            raw=canonical_catalog(payload)
            claims=dict(iss=self.signer.issuer,aud=self.signer.audience,caller=self.signer.caller,sub=context['actor_id'],tenant_id=context['tenant_id'],
                membership_id=context['membership_id'],session_id=context['session_id'],access_revision=context['access_revision'],action='trips.list',
                object_kind='trip_collection',object_id=None,object_version=1,operation_id=operation,request_hash=catalog_hash(payload),jti=str(uuid4()),
                iat=now,nbf=now,exp=expiry,human_receipt_id=None,canonical_version=2)
            token=self.signer.sign(claims,now=now)
            headers={'Authorization':'Bearer '+self.target.dispatcher_credential,'X-EAI-Delegation':token,'X-EAI-Operation-ID':operation,
                'X-EAI-Catalog-Payload':base64.urlsafe_b64encode(raw).decode().rstrip('=')}
        except Exception:raise TripFailure(503) from None
        try:
            async with asyncio.timeout(6):
                async with self.http.stream('GET','/service/v1/trips',params={k:v for k,v in query.items() if v is not None},headers=headers) as response:
                    if response.status_code!=200:raise TripFailure({401:403,403:403,404:404,409:409}.get(response.status_code,503))
                    if response.headers.get('content-type','').split(';')[0]!='application/json' or response.headers.get('content-encoding','identity')!='identity':raise TripFailure(503)
                    body=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body)+len(chunk)>32768:raise TripFailure(503)
                        body.extend(chunk)
            def unique(items):
                result={}
                for key,value in items:
                    if key in result:raise ValueError
                    result[key]=value
                return result
            value=json.loads(bytes(body).decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            result=page(value,after,limit)
            if self.clock()>=expiry:raise TripFailure(503)
            return result,expiry
        except TripFailure:raise
        except Exception:raise TripFailure(503) from None
