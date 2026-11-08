import asyncio
import copy
import time
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.workspace.domain import query,digest,projection
from enterprise_ai_platform.modules.workspace.service import WorkspaceService
from enterprise_ai_platform.modules.actions.domain import ActionFailure
from enterprise_ai_platform.modules.delegation.domain import canonical_catalog

ACTOR=str(uuid4());TENANT=str(uuid4());CLAIM=str(uuid4())
def summary():return dict(id=CLAIM,owner_id=ACTOR,public_reference='REF-1',state='pending_approval',version=1,revision=1,active_stage=1,total_minor=500,currency='SGD',submitted_at='2026-11-06T00:00:00+00:00',needs_action=False)
def payload(view='mine',id=None):
    values=query(view,id,None,1)
    return dict(schema_version=2,action='workflow.read',tenant_id=TENANT,actor_id=ACTOR,operation_id=str(uuid4()),object=dict(kind='employee_workspace' if view=='mine' else 'approval_workspace',id=id,version=1),data=dict(after=None,limit=1,request_digest=digest(values)))

@pytest.mark.parametrize('limit',[True,0,21,1.5,'1',None])
def test_query_limit_strict(limit):
    with pytest.raises(ValueError):query('mine',None,None,limit)

@pytest.mark.parametrize('change',[lambda p:p['object'].update(kind='approval_workspace'),lambda p:p['object'].update(id=CLAIM),lambda p:p['data'].update(limit=2),lambda p:p['data'].update(after=CLAIM),lambda p:p['data'].update(confirm=True)])
def test_catalog_view_object_and_filters_cannot_reuse_digest(change):
    p=payload();change(p)
    with pytest.raises(ValueError):canonical_catalog(p)

def test_detail_and_collection_authority_are_distinct():
    p=payload(id=CLAIM);assert canonical_catalog(p)
    p['object']['id']=None
    with pytest.raises(ValueError):canonical_catalog(p)

@pytest.mark.parametrize('change',[lambda s:s.update(owner_id=str(uuid4())),lambda s:s.update(total_minor=True),lambda s:s.update(state='paid'),lambda s:s.update(needs_action=True),lambda s:s.update(extra='raw payload')])
def test_projection_rejects_foreign_and_invalid_states(change):
    s=summary();change(s)
    with pytest.raises(ValueError):projection(dict(items=[s],next_after=None),query('mine',None,None,1),ACTOR,TENANT)

def test_stale_or_foreign_cursor_and_duplicate_rows_are_not_accepted():
    s=summary()
    with pytest.raises(ValueError):projection(dict(items=[s],next_after=str(uuid4())),query('mine',None,None,1),ACTOR,TENANT)
    with pytest.raises(ValueError):projection(dict(items=[s,s],next_after=None),query('mine',None,None,2),ACTOR,TENANT)

def test_delivery_state_is_not_a_claim_state():
    s=summary();s['state']='committed'
    with pytest.raises(ValueError):projection(dict(items=[s],next_after=None),query('mine',None,None,1),ACTOR,TENANT)

class Repo:
    def __init__(self):self.calls=0
    async def call(self,*a,**kw):
        self.calls+=1
        return dict(tenant_id=TENANT,actor_id=ACTOR,membership_id=ACTOR,session_id=ACTOR,access_revision=1,authority_expires_at=time.time()+60)
class Reader:
    def __init__(self):self.calls=0
    async def read(self,ctx,values):
        self.calls+=1
        if self.calls==2:raise ActionFailure(404)
        return dict(items=[summary()],next_after=None),int(time.time())+50

def test_second_source_revocation_does_not_return_first_snapshot():
    async def exercise():
        repo=Repo();reader=Reader();service=WorkspaceService(repo,reader,None)
        with pytest.raises(ActionFailure) as error:await service.read('x'*43,'mine')
        assert error.value.status==404 and reader.calls==2
    asyncio.run(exercise())

def test_final_role_epoch_change_discards_second_source_snapshot():
    class Changed(Repo):
        async def call(self,*a,**kw):
            value=await super().call(*a,**kw)
            if self.calls==3:value['access_revision']=2
            return value
    class Stable(Reader):
        async def read(self,*a):self.calls+=1;return dict(items=[],next_after=None),int(time.time())+50
    async def exercise():
        reader=Stable();repo=Changed()
        with pytest.raises(ActionFailure) as error:await WorkspaceService(repo,reader,None).read('x'*43,'mine')
        assert error.value.status==409 and reader.calls==2 and repo.calls==3
    asyncio.run(exercise())


@pytest.mark.parametrize('value',['detail\x01kept','  exact\r\n\t whitespace  ','x'*4096,'é'*2048])
def test_reason_matches_exact_accepted_v1_contract(value):
    from enterprise_ai_platform.modules.workspace.domain import reason
    reason(value)

@pytest.mark.parametrize('value',['nul\x00denied','x'*4097,'é'*2049,True,None])
def test_reason_rejects_nul_over_budget_and_nonstring(value):
    from enterprise_ai_platform.modules.workspace.domain import reason
    with pytest.raises(ValueError):reason(value)
