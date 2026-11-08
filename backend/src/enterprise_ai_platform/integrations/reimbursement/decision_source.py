from enterprise_ai_platform.integrations.security.tls import internal_tls
"""Current reviewer source proof; source facts alone never record a decision."""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import date
from uuid import uuid4
import httpx
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyTarget,TransportLog
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey
from enterprise_ai_platform.modules.actions.domain import ActionFailure,VerifiedDecisionSource,encoded,exact,identifier,integer,digest
from enterprise_ai_platform.modules.delegation.ports import canonical_payload


class DecisionSourceReader:
    def __init__(self,target:PolicyTarget,signer,key:SettlementKey,credential_hash,*,transport=None,clock=time.time):
        self.target,self.signer,self.key,self.credential_hash,self.clock=target,signer,key,credential_hash,clock
        for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2'):
            logger=logging.getLogger(name)
            if not any(isinstance(f,TransportLog) for f in logger.filters):logger.addFilter(TransportLog())
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,follow_redirects=False,timeout=httpx.Timeout(5,connect=2),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))

    async def close(self):await self.http.aclose()

    async def read(self,context,claim_id):
        try:
            original={k:context[k] for k in ('tenant_id','actor_id','membership_id','session_id','access_revision')}
            for k in ('tenant_id','actor_id','membership_id','session_id'):identifier(original[k])
            integer(original['access_revision']);identifier(claim_id)
            stamp=int(self.clock());expiry=min(stamp+60,int(context['authority_expires_at']),self.key.not_after)
            if not self.key.not_before<=self.clock()<expiry:raise ValueError
            payload=dict(schema_version=1,action='claim.read',tenant_id=original['tenant_id'],actor_id=original['actor_id'],operation_id=str(uuid4()),object=dict(kind='claim',id=claim_id,version=1),data={})
            payload=json.loads(canonical_payload(payload));binding=hashlib.sha256(b'eai:human-action:v1:'+canonical_payload(payload)).hexdigest()
            claims=dict(iss=self.signer.issuer,aud=self.signer.audience,caller=self.signer.caller,sub=original['actor_id'],tenant_id=original['tenant_id'],membership_id=original['membership_id'],session_id=original['session_id'],access_revision=original['access_revision'],action='claim.read',object_kind='claim',object_id=claim_id,object_version=1,operation_id=payload['operation_id'],request_hash=binding,jti=str(uuid4()),iat=stamp,nbf=stamp,exp=expiry,human_receipt_id=None)
            headers={'authorization':'Bearer '+self.target.dispatcher_credential,'x-eai-delegation':self.signer.sign(claims,now=stamp),'content-type':'application/json'}
            async with asyncio.timeout(8):
                async with self.http.stream('POST','/service/v1/claims/'+claim_id+'/decision-source',content=encoded(dict(payload=payload)),headers=headers) as response:
                    if response.status_code!=200:raise ActionFailure({401:403,403:403,404:404,409:409}.get(response.status_code,503))
                    if response.headers.get('content-type','').split(';')[0]!='application/json' or response.headers.get('content-encoding','identity')!='identity':raise ValueError
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw)+len(chunk)>16384:raise ValueError
                        raw.extend(chunk)
            def unique(rows):
                result={}
                for k,v in rows:
                    if k in result:raise ValueError
                    result[k]=v
                return result
            value=json.loads(bytes(raw),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            exact(value,{'tenant_id','actor_id','operation_id','request_hash','claim','checked_at','expires_at'})
            if any(value[k]!=v for k,v in dict(tenant_id=original['tenant_id'],actor_id=original['actor_id'],operation_id=payload['operation_id'],request_hash=binding).items()):raise ValueError
            integer(value['checked_at']);integer(value['expires_at'])
            if not value['checked_at']<=self.clock()+5 or not value['checked_at']<value['expires_at'] or not self.clock()<value['expires_at']<=min(expiry,value['checked_at']+60):raise ValueError
            claim=value['claim'];exact(claim,{'id','owner_id','trip_id','trip_date','state','version','revision','active_stage','reviewer_id','policy'})
            if (claim['id'],claim['state'],claim['reviewer_id'])!=(claim_id,'pending_approval',original['actor_id']) or claim['owner_id']==original['actor_id']:raise ActionFailure(403)
            for k in ('id','owner_id','trip_id','reviewer_id'):identifier(claim[k])
            for k in ('version','revision'):integer(claim[k])
            integer(claim['active_stage'],10)
            if date.fromisoformat(claim['trip_date']).isoformat()!=claim['trip_date']:raise ValueError
            exact(claim['policy'],{'id','version','digest'});identifier(claim['policy']['id']);integer(claim['policy']['version']);digest(claim['policy']['digest'])
            material=dict(schema_version=1,context=original,claim=claim,checked_at=value['checked_at'],expires_at=value['expires_at'])
            proof=hmac.new(self.key.material,b'eai:decision-source:v1:'+encoded(material),hashlib.sha256).hexdigest()
            if self.clock()>=value['expires_at']:raise ValueError
            return VerifiedDecisionSource(encoded(dict(credential_hash=self.credential_hash,key_id=self.key.kid,material=material,proof=proof)))
        except ActionFailure:raise
        except Exception:raise ActionFailure(503) from None
