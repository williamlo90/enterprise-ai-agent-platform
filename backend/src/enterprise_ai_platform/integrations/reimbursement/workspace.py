from enterprise_ai_platform.integrations.security.tls import internal_tls
"""Fixed private endpoint and exact v2 query binding; no arbitrary URL/tool."""
import asyncio
import hashlib
import base64
import json
import time
from uuid import uuid4
import httpx
from enterprise_ai_platform.modules.workspace.domain import digest,projection
from enterprise_ai_platform.modules.actions.domain import ActionFailure
from enterprise_ai_platform.modules.delegation.domain import canonical_catalog

class WorkspaceReader:
    def __init__(self,target,signer,*,transport=None,clock=time.time):
        self.target,self.signer,self.clock=target,signer,clock
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,follow_redirects=False,timeout=httpx.Timeout(5,connect=2),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))
    async def close(self):await self.http.aclose()
    async def read(self,context,values):
        try:
            operation=str(uuid4());stamp=int(self.clock());expiry=min(stamp+60,int(context['authority_expires_at']));request_digest=digest(values)
            if self.clock()>=expiry:raise ValueError
            kind='employee_workspace' if values['view']=='mine' else 'approval_workspace'
            payload=dict(schema_version=2,action='workflow.read',tenant_id=context['tenant_id'],actor_id=context['actor_id'],operation_id=operation,object=dict(kind=kind,id=values['claim_id'],version=1),data=dict(after=values['after'],limit=values['limit'],request_digest=request_digest))
            claims=dict(iss=self.signer.issuer,aud=self.signer.audience,caller=self.signer.caller,sub=context['actor_id'],tenant_id=context['tenant_id'],membership_id=context['membership_id'],session_id=context['session_id'],access_revision=context['access_revision'],action='workflow.read',object_kind=kind,object_id=values['claim_id'],object_version=1,operation_id=operation,request_hash=hashlib.sha256(b'eai:catalog-action:v2:'+canonical_catalog(payload)).hexdigest(),jti=str(uuid4()),iat=stamp,nbf=stamp,exp=expiry,human_receipt_id=None,canonical_version=2)
            headers={'authorization':'Bearer '+self.target.dispatcher_credential,'x-eai-delegation':self.signer.sign(claims,now=stamp),'x-eai-catalog-payload':base64.urlsafe_b64encode(canonical_catalog(payload)).decode().rstrip('='),'content-type':'application/json'}
            async with asyncio.timeout(8):
                async with self.http.stream('POST','/service/v1/workspace',json=dict(values,operation_id=operation),headers=headers) as response:
                    if response.status_code!=200:raise ActionFailure({401:403,403:403,404:404,409:409}.get(response.status_code,503))
                    if response.headers.get('content-type','').split(';')[0]!='application/json' or response.headers.get('content-encoding','identity')!='identity':raise ValueError
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw)+len(chunk)>262144:raise ValueError
                        raw.extend(chunk)
            def pairs(rows):
                result={}
                for k,v in rows:
                    if k in result:raise ValueError
                    result[k]=v
                return result
            value=json.loads(bytes(raw),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if type(value) is not dict or set(value)!={'tenant_id','actor_id','operation_id','request_hash','checked_at','expires_at','result'} or any(value[k]!=v for k,v in dict(tenant_id=context['tenant_id'],actor_id=context['actor_id'],operation_id=operation,request_hash=request_digest).items()):raise ValueError
            if type(value['checked_at']) is not int or type(value['expires_at']) is not int or not value['checked_at']<=self.clock()+5 or not value['checked_at']<value['expires_at']<=min(expiry,value['checked_at']+60) or self.clock()>=value['expires_at']:raise ValueError
            return projection(value['result'],values,context['actor_id'],context['tenant_id']),value['expires_at']
        except ActionFailure:raise
        except Exception:raise ActionFailure(503) from None
