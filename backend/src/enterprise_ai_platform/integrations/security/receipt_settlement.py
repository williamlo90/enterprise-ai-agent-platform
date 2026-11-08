"""Private settlement MAC after independent signature verification.

The database registry has the same operator-provisioned key; runtime SQL cannot
read it or settle a fabricated terminal merely by supplying a shaped dictionary.
"""
from dataclasses import dataclass,field
import hmac
import hashlib
import re
import time
from enterprise_ai_platform.modules.receipts.domain import ReceiptFailure,settlement_material,encoded,live


@dataclass(frozen=True)
class SettlementKey:
    kid:str
    material:bytes=field(repr=False)
    not_before:int
    not_after:int

    def __post_init__(self):
        if (type(self.kid) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}',self.kid) or type(self.material) is not bytes or len(self.material)!=32
                or type(self.not_before) is not int or type(self.not_after) is not int or not 0<=self.not_before<self.not_after):
            raise ValueError('Invalid receipt settlement configuration.')


class ReceiptSettlement:
    def __init__(self,key:SettlementKey=None,*,clock=time.time):
        if key is not None and type(key) is not SettlementKey:raise ValueError('Invalid settlement key.')
        self.key,self.clock=key,clock

    def issue(self,batch,evidence):
        try:
            now=self.clock()
            if self.key is None or not self.key.not_before<=now<self.key.not_after:raise ValueError
            live(evidence.expires_at,now)
            if evidence.expires_at>self.key.not_after:raise ValueError
            material=settlement_material(batch,evidence)
            signature=hmac.new(self.key.material,b'eai:receipt-settlement:v1:'+encoded(material),hashlib.sha256).hexdigest()
            if self.clock()>=min(evidence.expires_at,self.key.not_after):raise ValueError
            return dict(key_id=self.key.kid,proof=signature,material=material)
        except Exception:raise ReceiptFailure(503) from None
