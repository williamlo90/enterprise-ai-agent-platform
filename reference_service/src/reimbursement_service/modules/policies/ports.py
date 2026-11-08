from typing import Protocol
from reimbursement_service.modules.policies.domain import Package,canonical_body,request_digest,encoded


class PackagesRepository(Protocol):
    async def receive(self,package:Package,key,authority)->dict: ...
