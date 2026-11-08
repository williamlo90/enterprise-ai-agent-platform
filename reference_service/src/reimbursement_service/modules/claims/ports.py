from typing import Protocol
from reimbursement_service.modules.claims.domain import ClaimFailure,Financial,ClaimState,ClaimPermit


class ClaimsRepository(Protocol):
    async def facts(self,trip_id,policy,actor,*,item_ids=())->dict: ...
    async def read(self,claim_id,actor)->dict: ...
    async def relation(self,claim_id,actor)->dict: ...
    async def apply(self,uow,operation:str,packet:dict)->dict:
        """Apply within the caller's REF-03 ledger transaction; no new UoW."""
        ...


class ClaimsAuthority(Protocol):
    async def authorize(self,payload,actor,*,operation_key,candidate_claim_id,policy,trip_date,claim,context)->ClaimPermit:
        """Private verified consent/policy/pin evidence; never caller-provided flags."""
        ...
