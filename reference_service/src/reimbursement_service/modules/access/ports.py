from typing import Protocol
from reimbursement_service.modules.access.domain import VerifiedActor
from reimbursement_service.modules.access.domain import canonical_payload,request_hash
from reimbursement_service.modules.access.domain import CatalogAuthority,CatalogBinding,AccessFailure,FailureKind


class ActorVerifier(Protocol):
    async def verify(self, delegation: str, payload: dict) -> VerifiedActor: ...


class ScopedAuthorityPort(Protocol):
    async def authorize(self,headers,binding:CatalogBinding)->CatalogAuthority: ...

from reimbursement_service.modules.access.domain import identifier,number
