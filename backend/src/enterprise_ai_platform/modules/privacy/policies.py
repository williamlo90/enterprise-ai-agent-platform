"""Publication requires a trusted current approver; no unsigned activation flag."""
import time
from enterprise_ai_platform.modules.privacy.domain import Policy,PolicyDraft,PrivacyFailure


class PrivacyPolicyService:
    def __init__(self,repository,publisher,*,clock=time.time):
        self.repository,self.publisher,self.clock=repository,publisher,clock

    async def publish(self,credential,candidate):
        if type(candidate) is not PolicyDraft:raise PrivacyFailure('invalid')
        candidate.validate(self.clock())
        approved=await self.publisher.approve(credential,candidate)
        if (type(approved) is not Policy or any(getattr(approved,k)!=getattr(candidate,k) for k in ('tenant_id','revision','expires_at','rules'))):raise PrivacyFailure('denied')
        approved.validate(self.clock())
        # Project the producer's committed decision, not a request-time allow.
        # Browser/API publication stays disabled until that authoritative
        # approval producer and its verifiable durable receipt are implemented.
        return await self.repository.append(approved)
