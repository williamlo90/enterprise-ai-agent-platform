from typing import Protocol
from enterprise_ai_platform.modules.trips.domain import TripFailure


class TripRepository(Protocol):
    async def context(self,token,operation)->dict: ...


class TripReader(Protocol):
    async def listing(self,context,after,limit)->tuple[dict,int]: ...


class TripPort(Protocol):
    async def listing(self,session,*,after=None,limit=20)->dict: ...
