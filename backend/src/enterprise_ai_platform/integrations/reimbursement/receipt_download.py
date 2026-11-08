"""Current source relationship attestation, bound to the original browser identity."""
from dataclasses import replace
import hashlib
import hmac
import time
from enterprise_ai_platform.modules.receipts.domain import ClaimAccess,ReceiptFailure,encoded,digest,integer,identifier,live
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey


class ClaimDownloadAuthority:
    def __init__(self,reader,key:SettlementKey,credential_hash:str,*,clock=time.time):
        if type(key) is not SettlementKey:raise ValueError('Invalid receipt access key.')
        digest(credential_hash)
        self.reader,self.key,self.credential_hash,self.clock=reader,key,credential_hash,clock

    async def authorize(self,context,claim_id):
        try:
            original={k:context[k] for k in ('tenant_id','actor_id','membership_id','session_id','access_revision')}
            for k in ('tenant_id','actor_id','membership_id','session_id'):identifier(original[k])
            integer(original['access_revision']);identifier(claim_id)
            context=dict(original,authority_expires_at=context['authority_expires_at'])
            access=await self.reader.access(context,claim_id)
            if type(access) is not ClaimAccess or (access.tenant_id,access.actor_id,access.claim_id)!=(original['tenant_id'],original['actor_id'],claim_id):raise ValueError
            expires=min(access.expires_at,int(context['authority_expires_at']),self.key.not_after)
            if not self.key.not_before<=self.clock()<self.key.not_after:raise ValueError
            access=replace(access,expires_at=expires);live(expires,self.clock())
            material=dict(schema_version=1,context=original,access=access.wire())
            proof=hmac.new(self.key.material,b'eai:receipt-download:v1:'+encoded(material),hashlib.sha256).hexdigest()
            live(expires,self.clock())
            return access,dict(credential_hash=self.credential_hash,key_id=self.key.kid,material=material,proof=proof)
        except ReceiptFailure:raise
        except Exception:raise ReceiptFailure(503) from None
