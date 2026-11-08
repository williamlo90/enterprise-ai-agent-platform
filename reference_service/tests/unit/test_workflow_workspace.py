import copy
import hashlib
import json
from uuid import uuid4
import pytest
from reimbursement_service.modules.access.domain import canonical_catalog,CatalogBinding
from reimbursement_service.infrastructure.database.migration import revisions

def payload(view='mine',id=None):
    values=dict(view=view,claim_id=id,after=None,limit=1)
    digest=hashlib.sha256(b'reimbursement:workspace:v1:'+json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return dict(schema_version=2,action='workflow.read',actor_id=str(uuid4()),tenant_id=str(uuid4()),operation_id=str(uuid4()),object=dict(kind='employee_workspace' if view=='mine' else 'approval_workspace',id=id,version=1),data=dict(after=None,limit=1,request_digest=digest))

@pytest.mark.parametrize('view',['mine','approvals'])
@pytest.mark.parametrize('detail',[False,True])
def test_exact_workspace_catalog_contract(view,detail):
    p=payload(view,str(uuid4()) if detail else None);assert json.loads(canonical_catalog(p))==p
    assert CatalogBinding('workflow.read',p['object']['id'],1,p['data']['request_digest'],p['operation_id'],None)

@pytest.mark.parametrize('mutate',[lambda p:p['object'].update(kind='trip_collection'),lambda p:p['object'].update(kind='approval_workspace'),lambda p:p['data'].update(limit=True),lambda p:p['data'].update(after=str(uuid4())),lambda p:p['object'].update(id=str(uuid4())),lambda p:p['data'].update(role='approver')])
def test_view_or_filter_confusion_rejected(mutate):
    p=payload();mutate(p)
    with pytest.raises(ValueError):canonical_catalog(p)

def test_detail_grant_is_not_a_mutation_grant():
    p=payload('approvals',str(uuid4()));p['action']='claim.decide'
    with pytest.raises(ValueError):canonical_catalog(p)

def test_source_revision6_is_query_only():
    revision=revisions()[-1]
    assert revision.name=='0006_workflow_workspace.sql'
    assert 'GRANT EXECUTE ON FUNCTION {schema}.workspace_call(text,jsonb)' in revision.source
    assert 'GRANT SELECT' not in revision.source and 'UPDATE {schema}.claims' not in revision.source
