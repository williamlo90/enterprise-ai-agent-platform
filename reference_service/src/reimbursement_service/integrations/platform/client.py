from reimbursement_service.integrations.security.tls import internal_tls
"""Bounded, nonredirecting TLS client with exact actor response validation."""
import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
import time
from urllib.parse import urlsplit
import httpx
from reimbursement_service.modules.access.domain import canonical_payload, request_hash, identifier, number
from reimbursement_service.modules.access.domain import AccessFailure, FailureKind, VerifiedActor


def unique_json(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError
        result[key]=value
    return result


class SafeTransportLog(logging.Filter):
    def filter(self,record):
        record.msg,record.args='Platform HTTP transport event.',()
        record.exc_info=record.exc_text=record.stack_info=None
        return True


@dataclass(frozen=True)
class PlatformTarget:
    origin: str
    credential: str = field(repr=False)

    def __post_init__(self):
        parts=urlsplit(self.origin)
        if (parts.scheme!='https' or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}',self.credential)):
            raise ValueError('Invalid platform integration configuration.')


class PlatformClient:
    def __init__(self,target:PlatformTarget,*,transport=None,clock=time.time):
        self.target,self.clock=target,clock
        for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2'):
            logger=logging.getLogger(name)
            if not any(isinstance(f,SafeTransportLog) for f in logger.filters):logger.addFilter(SafeTransportLog())
        # Explicit transport is only for controlled tests. Production uses TLS
        # verification, no proxy environment, no redirects and no retry policy.
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,
            follow_redirects=False,timeout=httpx.Timeout(5,connect=2),
            limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))

    async def close(self): await self.http.aclose()

    async def call(self,path,body,*,delegation=None):
        if not path.startswith('/internal/v1/') or '?' in path or '#' in path or '..' in path:
            raise AccessFailure(FailureKind.INVALID)
        headers={'Authorization':'Bearer '+self.target.credential,'Content-Type':'application/json'}
        if delegation is not None:
            if (not isinstance(delegation,str) or not delegation or len(delegation)>16384
                    or any(ord(c)<33 or ord(c)>126 for c in delegation)):
                raise AccessFailure(FailureKind.UNAUTHENTICATED)
            headers['X-EAI-Delegation']=delegation
        try:
            raw=json.dumps(body,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
            if len(raw)>131072: raise ValueError
        except (ValueError,TypeError,UnicodeError): raise AccessFailure(FailureKind.INVALID) from None
        try:
            async with asyncio.timeout(6):
                async with self.http.stream('POST',path,content=raw,headers=headers) as response:
                    if response.status_code!=200:
                        kind={400:FailureKind.INVALID,401:FailureKind.UNAUTHENTICATED,
                              403:FailureKind.FORBIDDEN,409:FailureKind.CONFLICT}.get(response.status_code)
                        if kind is None: kind=FailureKind.UNAVAILABLE if response.status_code in {429,502,503,504} else FailureKind.PROTOCOL
                        raise AccessFailure(kind)
                    if (response.headers.get('content-type','').split(';')[0]!='application/json'
                            or response.headers.get('content-encoding','identity')!='identity'):
                        raise AccessFailure(FailureKind.PROTOCOL)
                    buffer=bytearray()
                    async for chunk in response.aiter_raw():
                        if len(buffer)+len(chunk)>65536: raise AccessFailure(FailureKind.PROTOCOL)
                        buffer.extend(chunk)
            value=json.loads(bytes(buffer).decode('utf-8'),object_pairs_hook=unique_json,
                             parse_constant=lambda value:(_ for _ in ()).throw(ValueError()))
            if type(value) is not dict: raise ValueError
            return value
        except (httpx.HTTPError,TimeoutError): raise AccessFailure(FailureKind.UNAVAILABLE) from None
        except (ValueError,UnicodeError): raise AccessFailure(FailureKind.PROTOCOL) from None

    async def verify(self,delegation,payload):
        try:
            # Round-trip freezes mutable caller material before the HTTP await.
            frozen=json.loads(canonical_payload(payload))
            digest=request_hash(frozen)
        except (ValueError,TypeError,KeyError): raise AccessFailure(FailureKind.INVALID) from None
        result=await self.call('/internal/v1/actors/verify',{'payload':frozen},delegation=delegation)
        try:
            if set(result)!={'allow','scope','actor_id','tenant_id','membership_id','access_revision',
                    'action','operation_id','request_hash','verified_at','expires_at','replayed'}:
                raise ValueError
            if result['allow'] is not True or result['scope']!='actor_only': raise ValueError
            for key in ('actor_id','tenant_id','membership_id','operation_id'): identifier(result[key])
            for key in ('access_revision','verified_at','expires_at'): number(result[key])
            if (type(result['replayed']) is not bool or result['request_hash']!=digest
                    or any(result[k]!=frozen[k] for k in ('actor_id','tenant_id','action','operation_id'))
                    or not result['verified_at']<=self.clock()+5
                    or result['expires_at']<=result['verified_at']
                    or not self.clock()<result['expires_at']<=result['verified_at']+60): raise ValueError
            return VerifiedActor(**{k:v for k,v in result.items() if k!='allow'})
        except (ValueError,TypeError,KeyError): raise AccessFailure(FailureKind.PROTOCOL) from None
