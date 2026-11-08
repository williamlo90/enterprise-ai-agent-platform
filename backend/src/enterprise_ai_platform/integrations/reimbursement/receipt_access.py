from enterprise_ai_platform.integrations.security.tls import internal_tls
"""Bounded private claim relationship lookup; no role or browser DTO is trusted."""
import asyncio
import json
import logging
import math
import time
from uuid import uuid4
import httpx
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget,TransportLog
from enterprise_ai_platform.modules.delegation.domain import canonical_payload,request_hash
from enterprise_ai_platform.modules.receipts.domain import ClaimAccess,ReceiptFailure,receipt_refs,identifier,integer,exact,live,encoded


class ClaimReceiptReader:
    def __init__(self,target:PolicyTarget,signer,*,transport=None,clock=time.time):
        self.target,self.signer,self.clock=target,signer,clock
        for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2'):
            logger=logging.getLogger(name)
            if not any(isinstance(f,TransportLog) for f in logger.filters):logger.addFilter(TransportLog())
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(4,connect=2),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))

    async def close(self):await self.http.aclose()

    async def access(self,context,claim_id):
        try:
            identifier(claim_id)
            # Copy only authoritative context fields before any external await.
            context={k:context[k] for k in ('tenant_id','actor_id','membership_id','session_id','access_revision','authority_expires_at')}
            for k in ('tenant_id','actor_id','membership_id','session_id'):identifier(context[k])
            integer(context['access_revision'])
            deadline=context['authority_expires_at'];now=int(self.clock())
            if type(deadline) not in (int,float) or not math.isfinite(deadline):raise ValueError
            expiry=min(now+60,int(deadline));live(expiry,self.clock())
            operation=str(uuid4())
            payload=dict(schema_version=1,action='claim.read',tenant_id=context['tenant_id'],actor_id=context['actor_id'],operation_id=operation,
                object=dict(kind='claim',id=claim_id,version=1),data={})
            payload=json.loads(canonical_payload(payload));binding=request_hash(payload)
            claims=dict(iss=self.signer.issuer,aud=self.signer.audience,caller=self.signer.caller,sub=context['actor_id'],tenant_id=context['tenant_id'],
                membership_id=context['membership_id'],session_id=context['session_id'],access_revision=context['access_revision'],action='claim.read',
                object_kind='claim',object_id=claim_id,object_version=1,operation_id=operation,request_hash=binding,jti=str(uuid4()),
                iat=now,nbf=now,exp=expiry,human_receipt_id=None)
            token=self.signer.sign(claims,now=now)
            headers={'Authorization':'Bearer '+self.target.dispatcher_credential,'X-EAI-Delegation':token,'Content-Type':'application/json'}
        except Exception:raise ReceiptFailure(503) from None
        try:
            async with asyncio.timeout(6):
                async with self.http.stream('POST','/service/v1/claims/'+claim_id+'/access-check',content=encoded({'payload':payload}),headers=headers) as response:
                    if response.status_code!=200:raise ReceiptFailure({401:403,403:403,404:404,409:409}.get(response.status_code,503))
                    if response.headers.get('content-type','').split(';')[0]!='application/json' or response.headers.get('content-encoding','identity')!='identity':raise ValueError
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw)+len(chunk)>8192:raise ValueError
                        raw.extend(chunk)
            def unique(pairs):
                result={}
                for k,v in pairs:
                    if k in result:raise ValueError
                    result[k]=v
                return result
            result=json.loads(bytes(raw).decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            exact(result,{'allow','tenant_id','actor_id','claim_id','claim_version','revision','receipts','operation_id','request_hash','checked_at','expires_at'})
            if result['allow'] is not True or any(result[k]!=v for k,v in dict(tenant_id=context['tenant_id'],actor_id=context['actor_id'],claim_id=claim_id,operation_id=operation,request_hash=binding).items()):raise ValueError
            integer(result['checked_at']);integer(result['expires_at'])
            if not result['checked_at']<=self.clock()+5 or not self.clock()<result['expires_at']<=min(expiry,result['checked_at']+60) or result['expires_at']<=result['checked_at']:raise ValueError
            access=ClaimAccess(result['tenant_id'],result['actor_id'],result['claim_id'],result['claim_version'],result['revision'],receipt_refs(result['receipts']),result['expires_at'])
            live(access.expires_at,self.clock())
            return access
        except ReceiptFailure:raise
        except Exception:raise ReceiptFailure(503) from None
