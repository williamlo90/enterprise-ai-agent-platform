from enterprise_ai_platform.integrations.security.tls import internal_tls
"""Current owner source facts over v1 signed reads; no public financial writer."""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from uuid import uuid4
import httpx
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget,TransportLog
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey
from enterprise_ai_platform.modules.delegation.domain import canonical_payload,request_hash
from enterprise_ai_platform.modules.drafts.domain import VerifiedDraftSource,DraftFailure,encoded,exact,identifier,integer,source_trip


class DraftSourceReader:
    def __init__(self,target:PolicyTarget,signer,key:SettlementKey,credential_hash,*,transport=None,clock=time.time):
        self.target,self.signer,self.key,self.credential_hash,self.clock=target,signer,key,credential_hash,clock
        for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2'):
            logger=logging.getLogger(name)
            if not any(isinstance(f,TransportLog) for f in logger.filters):logger.addFilter(TransportLog())
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,follow_redirects=False,timeout=httpx.Timeout(5,connect=2),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))

    async def close(self):await self.http.aclose()

    async def read(self,context,trip_id,claim_id):
        try:
            original={k:context[k] for k in ('tenant_id','actor_id','membership_id','session_id','access_revision')}
            for k in ('tenant_id','actor_id','membership_id','session_id'):identifier(original[k])
            integer(original['access_revision']);identifier(trip_id)
            if claim_id is not None:identifier(claim_id)
            stamp=int(self.clock());deadline=min(stamp+60,int(context['authority_expires_at']),self.key.not_after)
            if not self.key.not_before<=self.clock()<deadline:raise ValueError
            action='trip.read' if claim_id is None else 'claim.read';obj=dict(kind='trip' if claim_id is None else 'claim',id=trip_id if claim_id is None else claim_id,version=1)
            payload=dict(schema_version=1,action=action,tenant_id=original['tenant_id'],actor_id=original['actor_id'],operation_id=str(uuid4()),object=obj,data={})
            payload=json.loads(canonical_payload(payload));digest=request_hash(payload)
            claims=dict(iss=self.signer.issuer,aud=self.signer.audience,caller=self.signer.caller,sub=original['actor_id'],tenant_id=original['tenant_id'],membership_id=original['membership_id'],session_id=original['session_id'],access_revision=original['access_revision'],action=action,object_kind=obj['kind'],object_id=obj['id'],object_version=1,operation_id=payload['operation_id'],request_hash=digest,jti=str(uuid4()),iat=stamp,nbf=stamp,exp=deadline,human_receipt_id=None)
            token=self.signer.sign(claims,now=stamp)
            headers={'authorization':'Bearer '+self.target.dispatcher_credential,'x-eai-delegation':token,'content-type':'application/json'}
            trip_payload=None
            if claim_id is not None:
                # A claim read never becomes trip-read authority. Verify both
                # independently, bound to the original browser identity.
                trip_payload={**payload,'action':'trip.read','operation_id':str(uuid4()),'object':dict(kind='trip',id=trip_id,version=1)}
                trip_claims={**claims,'action':'trip.read','object_kind':'trip','object_id':trip_id,'operation_id':trip_payload['operation_id'],'request_hash':request_hash(trip_payload),'jti':str(uuid4())}
                headers['x-eai-trip-delegation']=self.signer.sign(trip_claims,now=stamp)
            async with asyncio.timeout(8):
                async with self.http.stream('POST','/service/v1/draft-sources',content=encoded(dict(payload=payload,trip_id=trip_id,trip_payload=trip_payload)),headers=headers) as response:
                    if response.status_code!=200:raise DraftFailure({401:403,403:403,404:404,409:409}.get(response.status_code,503))
                    if response.headers.get('content-type','').split(';')[0]!='application/json' or response.headers.get('content-encoding','identity')!='identity':raise ValueError
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw)+len(chunk)>65536:raise ValueError
                        raw.extend(chunk)
            def unique(pairs):
                out={}
                for k,v in pairs:
                    if k in out:raise ValueError
                    out[k]=v
                return out
            value=json.loads(bytes(raw),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            exact(value,{'tenant_id','actor_id','operation_id','request_hash','trip','claim','checked_at','expires_at'})
            if any(value[k]!=v for k,v in dict(tenant_id=original['tenant_id'],actor_id=original['actor_id'],operation_id=payload['operation_id'],request_hash=digest).items()):raise ValueError
            integer(value['checked_at']);integer(value['expires_at'])
            if not value['checked_at']<=self.clock()+5 or not value['checked_at']<value['expires_at'] or not self.clock()<value['expires_at']<=min(deadline,value['checked_at']+60):raise ValueError
            trip=source_trip(value['trip'],trip_id);locked=value['claim']
            if claim_id is None:
                if locked is not None:raise ValueError
            else:
                exact(locked,{'id','owner_id','trip_id','state','version','revision','policy'})
                if (locked['id'],locked['owner_id'],locked['trip_id'],locked['state'])!=(claim_id,original['actor_id'],trip_id,'needs_information'):raise ValueError
                integer(locked['version']);integer(locked['revision']);exact(locked['policy'],{'id','version','digest'})
                identifier(locked['policy']['id']);integer(locked['policy']['version'])
                if type(locked['policy']['digest']) is not str or len(locked['policy']['digest'])!=64 or any(c not in '0123456789abcdef' for c in locked['policy']['digest']):raise ValueError
            material=dict(schema_version=1,context=original,trip=trip,claim=locked,checked_at=value['checked_at'],expires_at=value['expires_at'])
            proof=hmac.new(self.key.material,b'eai:draft-source:v1:'+encoded(material),hashlib.sha256).hexdigest()
            if self.clock()>=value['expires_at']:raise ValueError
            return VerifiedDraftSource(encoded(dict(credential_hash=self.credential_hash,key_id=self.key.kid,material=material,proof=proof)))
        except DraftFailure:raise
        except Exception:raise DraftFailure(503) from None
