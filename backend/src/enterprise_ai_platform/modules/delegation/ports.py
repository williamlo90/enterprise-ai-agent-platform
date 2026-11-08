from typing import Protocol
from enterprise_ai_platform.modules.delegation.domain import HumanReceipt,VerifiedActor,canonical_payload


class DelegationFailure(RuntimeError):
    def __init__(self,status):
        self.status=status if status in {400,401,403,409,503} else 503
        super().__init__("Internal verification rejected.")


class DelegationRepositoryPort(Protocol):
    async def call(self,operation,payload): ...


class SignatureVerifierPort(Protocol):
    def header(self,token): ...
    def verify(self,token,key): ...


class HumanReceiptReaderPort(Protocol):
    async def get(self,receipt_id: str) -> HumanReceipt | None: ...


class DelegationServicePort(Protocol):
    async def verify(self,credential,delegation,payload): ...
    async def verify_private(self,credential,delegation,payload)->VerifiedActor: ...
