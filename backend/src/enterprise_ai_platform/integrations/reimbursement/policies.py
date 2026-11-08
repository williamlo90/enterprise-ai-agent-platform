from enterprise_ai_platform.integrations.security.tls import internal_tls
"""Exact REF-04 copy request with v2 publisher grant and bounded HTTP."""
import asyncio
import base64
from dataclasses import dataclass,field
import json
import logging
import hashlib
import hmac
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4
import httpx
from enterprise_ai_platform.modules.policies.domain import Package,PolicyFailure,ReceivedCopy,canonical_body,encoded,request_digest,validate_ack,settlement_material
from enterprise_ai_platform.modules.delegation.domain import catalog_hash,canonical_catalog


@dataclass(frozen=True)
class PolicyTarget:
    origin:str
    dispatcher_credential:str=field(repr=False)
    settlement_key_id:str='reference-v1'
    def __post_init__(self):
        parts=urlsplit(self.origin)
        if (parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.path or parts.query or parts.fragment
                or not re.fullmatch('[A-Za-z0-9_-]{43}',self.dispatcher_credential) or not re.fullmatch('[a-zA-Z0-9_.-]{1,64}',self.settlement_key_id)):
            raise ValueError('Invalid reimbursement integration configuration.')


class TransportLog(logging.Filter):
    def filter(self,record):
        record.msg,record.args='Reimbursement HTTP transport event.',()
        record.exc_info=record.exc_text=record.stack_info=None
        return True


class PolicyReceiver:
    def __init__(self,target:PolicyTarget,signer,*,transport=None,clock=time.time):
        self.target,self.signer,self.clock=target,signer,clock
        for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2'):
            logger=logging.getLogger(name)
            if not any(isinstance(f,TransportLog) for f in logger.filters):logger.addFilter(TransportLog())
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(4,connect=2),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))

    async def close(self):await self.http.aclose()

    async def receive(self,context,body,package_id,version,key,operation_id):
        try:
            package=Package(context['tenant_id'],package_id,version,canonical_body(body))
            payload=dict(schema_version=2,action='policy.publish',tenant_id=context['tenant_id'],actor_id=context['actor_id'],operation_id=operation_id,
                object=dict(kind='policy_package',id=package_id,version=version),data=dict(key=key,request_digest=request_digest(package_id,version,package.canonical)))
            raw_catalog=canonical_catalog(payload);now=int(self.clock());expiry=min(now+60,int(context['authority_expires_at']))
            if self.clock()>=expiry:raise ValueError
            claims=dict(iss=self.signer.issuer,aud=self.signer.audience,caller=self.signer.caller,sub=context['actor_id'],tenant_id=context['tenant_id'],
                membership_id=context['membership_id'],access_revision=context['access_revision'],session_id=context['session_id'],action='policy.publish',
                object_kind='policy_package',object_id=package_id,object_version=version,operation_id=operation_id,request_hash=catalog_hash(payload),
                jti=str(uuid4()),iat=now,nbf=now,exp=expiry,human_receipt_id=None,canonical_version=2)
            token=self.signer.sign(claims,now=now)
            headers={'Authorization':'Bearer '+self.target.dispatcher_credential,'X-EAI-Delegation':token,
                'X-EAI-Catalog-Payload':base64.urlsafe_b64encode(raw_catalog).decode().rstrip('='),'Content-Type':'application/json',
                'Idempotency-Key':key,'X-EAI-Operation-ID':operation_id}
        except Exception:raise PolicyFailure(503) from None
        try:
            async with asyncio.timeout(6):
                async with self.http.stream('PUT',f'/service/v1/policy-packages/{package_id}/versions/{version}',content=encoded(body),headers=headers) as response:
                    if response.status_code!=200:raise PolicyFailure({401:403,403:403,409:409}.get(response.status_code,503))
                    if response.headers.get('content-type','').split(';')[0]!='application/json' or response.headers.get('content-encoding','identity')!='identity':raise PolicyFailure(503)
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw)+len(chunk)>8192:raise PolicyFailure(503)
                        raw.extend(chunk)
            def unique(items):
                value={}
                for name,item in items:
                    if name in value:raise ValueError
                    value[name]=item
                return value
            result=json.loads(bytes(raw).decode('utf-8'),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            ack=validate_ack(result,package_id,version,package.digest)
            if self.clock()>=expiry or ack['received_at']>self.clock()+5:raise PolicyFailure(503)
            material=settlement_material(context['tenant_id'],context['actor_id'],package_id,version,key,operation_id,context['expected_version'],ack)
            root=hashlib.sha256(b'eai:policy-settlement:key:v1:'+self.target.dispatcher_credential.encode()).digest()
            proof=hmac.new(root,encoded(material),hashlib.sha256).hexdigest()
            return ReceivedCopy(ack,self.target.settlement_key_id,proof)
        except PolicyFailure:raise
        except Exception:raise PolicyFailure(503) from None
