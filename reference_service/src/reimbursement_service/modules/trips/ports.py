from typing import Protocol
from reimbursement_service.modules.trips.domain import Trip,ExpenseItem,identifier,business_date,amount,listing_digest


class TripsRepository(Protocol):
    async def detail(self,trip_id,actor)->dict: ...
    async def listing(self,after,limit,actor)->dict: ...
