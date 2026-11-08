from typing import Protocol
from reimbursement_service.modules.operations.domain import Identity,Result,Terminal
from reimbursement_service.modules.operations.domain import OperationFailure,uuid


class AtomicWork(Protocol):
    async def apply(self,uow) -> tuple[str,Result]:
        """Trusted feature effect on the SAME UoW; no external network calls."""
        ...


class OperationsRepository(Protocol):
    async def existing(self,identity:Identity,actor)->Terminal|None: ...
    async def commit(self,identity:Identity,actor,work:AtomicWork)->Terminal: ...
    async def seal(self,identity:Identity,actor)->Terminal: ...
    async def lookup(self,operation_id:str,actor)->Terminal: ...


class TerminalProof(Protocol):
    def issue(self,terminal:Terminal,*,authority_deadline:int)->dict: ...
