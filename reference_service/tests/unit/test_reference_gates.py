import asyncio
from dataclasses import replace
import json
import time
from uuid import uuid4
import pytest
from reimbursement_service.modules.access.domain import CatalogAuthority,AccessFailure,FailureKind
from reimbursement_service.modules.access.service import DefaultDenyScope
from reimbursement_service.modules.trips.service import TripsService
from reimbursement_service.modules.policies.service import PackagesService
from test_reference_domain import body


class Repository:
    def __init__(self):self.calls=0
    async def listing(self,*args):self.calls+=1;return {'items':[],'next_cursor':None}
    async def receive(self,*args):self.calls+=1;return {'state':'received'}


class Scopes:
    def __init__(self,tenant,actor,mode='ok'):self.tenant,self.actor,self.mode=tenant,actor,mode
    async def authorize(self,headers,binding):
        now=int(time.time())
        if self.mode=='denied':raise AccessFailure(FailureKind.FORBIDDEN)
        if self.mode=='down':raise AccessFailure(FailureKind.UNAVAILABLE)
        if self.mode=='wrong':binding=replace(binding,request_digest='f'*64)
        expires=now+60 if self.mode!='expired' else now-1
        verified=now if self.mode!='expired' else now-61
        return CatalogAuthority(binding,self.tenant,self.actor,'platform-dispatcher',verified,expires)


@pytest.mark.parametrize('port',['default','denied','down','wrong','expired'])
def test_gated_paths_fail_closed_before_repository(port):
    async def run():
        tenant,person,pid=str(uuid4()),str(uuid4()),str(uuid4());repository=Repository()
        scopes=DefaultDenyScope() if port=='default' else Scopes(tenant,person,port)
        trips=TripsService(repository,scopes);packages=PackagesService(repository,scopes)
        with pytest.raises(AccessFailure):await trips.listing([],str(uuid4()))
        with pytest.raises(AccessFailure):await packages.receive([],pid,1,str(uuid4()),str(uuid4()),body(tenant,pid))
        assert repository.calls==0
    asyncio.run(run())


def test_invalid_material_never_asks_producer_for_authority():
    async def run():
        class NoCall:
            async def authorize(self,*args):raise AssertionError('must validate shape first')
        repository=Repository();service=PackagesService(repository,NoCall())
        with pytest.raises(AccessFailure):await service.receive([],str(uuid4()),1,str(uuid4()),str(uuid4()),{'role':'publisher'})
        assert repository.calls==0
    asyncio.run(run())
