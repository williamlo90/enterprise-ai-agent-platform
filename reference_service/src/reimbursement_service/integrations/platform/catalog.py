"""Actual v2 HTTP authority adapter for the REF-04 scoped gate."""
import base64
import hashlib
import hmac
import re
from reimbursement_service.integrations.platform.client import unique_json
import json
from reimbursement_service.modules.access.domain import (
    canonical_catalog,catalog_hash,catalog_binding,CatalogAuthority,CatalogBinding,AccessFailure,FailureKind,identifier,number)


class CatalogAuthorityAdapter:
    def __init__(self,client,ingress_digest):
        if not isinstance(ingress_digest,str) or not re.fullmatch('[a-f0-9]{64}',ingress_digest):raise ValueError('Invalid catalog ingress configuration.')
        self.client,self.ingress_digest=client,ingress_digest

    async def authorize(self,headers,binding):
        try:
            if type(binding) is not CatalogBinding:raise ValueError
            values={}
            for key,value in headers:
                key=key.lower()
                if key in {b'authorization',b'x-eai-delegation',b'x-eai-catalog-payload',b'cookie'}:
                    if key in values:raise ValueError
                    values[key]=value.decode('ascii')
            if b'cookie' in values or not values.get(b'authorization','').startswith('Bearer '):raise ValueError
            secret=values[b'authorization'][7:]
            if not re.fullmatch('[A-Za-z0-9_-]{43}',secret):raise ValueError
            candidate=hashlib.sha256(b'reimbursement:dispatcher:v1:'+secret.encode()).hexdigest()
            if not hmac.compare_digest(candidate,self.ingress_digest):raise ValueError
            raw=values[b'x-eai-catalog-payload']
            if len(raw)>2731 or '=' in raw:raise ValueError
            decoded=base64.b64decode(raw+'='*((-len(raw))%4),altchars=b'-_',validate=True)
            if len(decoded)>2048 or base64.urlsafe_b64encode(decoded).decode().rstrip('=')!=raw:raise ValueError
            payload=json.loads(decoded.decode('utf-8'),object_pairs_hook=unique_json,
                parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            frozen=json.loads(canonical_catalog(payload))
            if catalog_binding(frozen)!=dict(action=binding.action,object_id=binding.object_id,object_version=binding.object_version,
                    request_digest=binding.request_digest,operation_id=binding.operation_id,key=binding.key):
                raise AccessFailure(FailureKind.FORBIDDEN)
            delegation=values[b'x-eai-delegation']
        except AccessFailure:raise
        except (ValueError,TypeError,KeyError,UnicodeError):raise AccessFailure(FailureKind.UNAUTHENTICATED) from None
        result=await self.client.call('/internal/v1/actors/verify',{'payload':frozen},delegation=delegation)
        try:
            if set(result)!={'allow','scope','canonical_version','actor_id','tenant_id','membership_id','access_revision',
                            'action','operation_id','request_hash','verified_at','expires_at','replayed','binding'}:raise ValueError
            if result['allow'] is not True or result['scope']!='catalog' or type(result['canonical_version']) is not int or result['canonical_version']!=2:raise ValueError
            for key in ('actor_id','tenant_id','membership_id','operation_id'):identifier(result[key])
            for key in ('access_revision','verified_at','expires_at'):number(result[key])
            returned_binding=CatalogBinding(**result['binding'])
            if (any(result[k]!=frozen[k] for k in ('actor_id','tenant_id','action','operation_id'))
                    or result['request_hash']!=catalog_hash(frozen) or returned_binding!=binding
                    or type(result['replayed']) is not bool or result['verified_at']>self.client.clock()+5
                    or not result['verified_at']<result['expires_at']<=result['verified_at']+60
                    or self.client.clock()>=result['expires_at']):raise ValueError
            authority=CatalogAuthority(binding,result['tenant_id'],result['actor_id'],'platform-dispatcher',result['verified_at'],result['expires_at'])
            if self.client.clock()>=authority.expires_at:raise ValueError
            return authority
        except (ValueError,TypeError,KeyError):raise AccessFailure(FailureKind.PROTOCOL) from None
