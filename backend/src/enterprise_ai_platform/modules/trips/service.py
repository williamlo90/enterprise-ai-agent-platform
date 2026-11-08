import math
import re
import time
from hashlib import sha256
from enterprise_ai_platform.modules.trips.domain import TripFailure,filters,page
from enterprise_ai_platform.modules.trips.ports import TripRepository,TripReader


class TripsService:
    def __init__(self,repository:TripRepository,reader:TripReader,*,clock=time.time):
        self.repository,self.reader,self.clock=repository,reader,clock

    def live(self,context):
        value=context.get('authority_expires_at')
        if type(value) not in (int,float) or not math.isfinite(value) or self.clock()>=value:raise TripFailure(401)

    async def listing(self,session,*,after=None,limit=20):
        if type(session) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}',session):raise TripFailure(401)
        try:filters(after,limit)
        except (ValueError,TypeError):raise TripFailure(400) from None
        token=sha256(('eai:session:v1:'+session).encode()).hexdigest()
        before=await self.repository.context(token,'context');self.live(before)
        value,deadline=await self.reader.listing(before,after,limit)
        if type(deadline) is not int or self.clock()>=deadline:raise TripFailure(503)
        result=page(value,after,limit)
        # The remote read holds no platform DB transaction. Bind its response to
        # the same session/tenant/member epoch after the last network await.
        current=await self.repository.context(token,'list_recheck');self.live(current);self.live(before)
        if self.clock()>=deadline:raise TripFailure(503)
        if any(current.get(key)!=before.get(key) for key in ('tenant_id','actor_id','membership_id','access_revision','session_id')):raise TripFailure(409)
        return result
