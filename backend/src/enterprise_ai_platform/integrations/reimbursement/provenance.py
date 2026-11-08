"""Request-local current claim proofs, never a persisted grant or arbitrary URL."""
import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass,field

@dataclass(frozen=True)
class ServicePermit:
    allowed:dict=field(repr=False)
    fingerprints:dict=field(repr=False)
    envelopes:tuple=field(repr=False)
    expires_at:float
    def __post_init__(self):
        if type(self.expires_at) not in (int,float) or not math.isfinite(self.expires_at) or type(self.allowed) is not dict or any(type(v) is not bool for v in self.allowed.values()) or any(v and r not in self.fingerprints for r,v in self.allowed.items()) or type(self.envelopes) is not tuple or len(self.envelopes)>8:raise ValueError('Invalid source verification.')

class ServiceProvenance:
    def __init__(self,reader,receipt_authority,*,clock=time.time):self.reader,self.receipt_authority,self.clock=reader,receipt_authority,clock
    async def verify(self,context,refs):
        allowed={r:False for r in refs if r.system=='service'};fingerprints={};envelopes=[]
        deadline=context.get('authority_expires_at',0)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or deadline<=self.clock():return ServicePermit(allowed,{},(),self.clock())
        expiry=min(deadline,self.clock()+10)
        anchors=[r for r in allowed if r.kind=='claim']
        if len(anchors)>8:return ServicePermit(allowed,{},(),expiry)
        try:
            async with asyncio.timeout(5):
                for ref in anchors:
                    if not re.fullmatch('[1-9][0-9]{0,17}',ref.version) or not re.fullmatch('[1-9][0-9]{0,17}',ref.observed_revision) or ref.page is not None or ref.chunk is not None:continue
                    try:
                        result,until=await self.reader.read(dict(context),dict(view='mine' if ref.relation=='owner' else 'approvals',claim_id=ref.resource_id,after=None,limit=1))
                        claim=result['claim'];actor=context['actor_id']
                        if claim['id']!=ref.resource_id or str(claim['version'])!=ref.version or str(claim['revision'])!=ref.observed_revision:continue
                        if ref.relation=='owner':
                            if claim['owner_id']!=actor:continue
                        elif claim['owner_id']==actor or claim['reviewer_id']!=actor or claim['state']!='pending_approval' or claim['active_stage'] is None:continue
                        access,envelope=await self.receipt_authority.authorize(dict(context),ref.resource_id)
                        if (access.tenant_id,access.actor_id,access.claim_id,access.claim_version,access.revision)!=(context['tenant_id'],actor,ref.resource_id,int(ref.version),int(ref.observed_revision)):continue
                        expiry=min(expiry,until,access.expires_at)
                        if self.clock()>=expiry:continue
                        allowed[ref]=True;fingerprints[ref]=hashlib.sha256(json.dumps(claim,sort_keys=True,separators=(',',':')).encode()).hexdigest();envelopes.append(envelope)
                        peers=[r for r in anchors if r.relation==ref.relation]
                        if len(peers)!=1:continue
                        receipts={r.id:r for r in access.receipts}
                        for receipt in allowed:
                            if receipt.kind!='receipt' or receipt.relation!=ref.relation or receipt.observed_revision!=ref.observed_revision or receipt.page is not None or receipt.chunk is not None:continue
                            item=receipts.get(receipt.resource_id)
                            if item is not None and str(item.version)==receipt.version:
                                allowed[receipt]=True;fingerprints[receipt]=(fingerprints[ref],item.digest)
                    except Exception:
                        continue
        except Exception:
            return ServicePermit({r:False for r in allowed},{},(),expiry)
        return ServicePermit(allowed,fingerprints,tuple(envelopes),expiry)
