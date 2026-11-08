import asyncio
import json
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.actions.domain import ActionFailure,VerifiedDecisionSource,encoded,identifiers
from enterprise_ai_platform.modules.actions.service import ActionService
from enterprise_ai_platform.modules.drafts.domain import VerifiedDraftSource


def uid():return str(uuid4())
HEADERS={'origin':'https://app.test','content-type':'application/json','x-csrf-token':'c'*43}


class Repository:
    def __init__(self):
        self.context=dict(tenant_id=uid(),actor_id=uid(),membership_id=uid(),session_id=uid(),access_revision=1,authority_expires_at=200)
        self.draft=dict(id=uid(),trip_id=uid(),claim_id=None);self.calls=[];self.replay=False;self.expiry=200;self.fail=False
    async def call(self,token,operation,values):
        self.calls.append((token,operation,values))
        if self.fail:raise ActionFailure(409)
        action=dict(operation_id=uid(),state='queued') if self.replay or operation=='read' or values.get('source') is not None else None
        return dict(action=action,context=self.context,draft=self.draft,authority_expires_at=self.expiry)


class Source:
    def __init__(self,decision=False):self.calls=[];self.decision=decision;self.hook=lambda:None
    async def read(self,*args):
        self.calls.append(args);self.hook()
        cls=VerifiedDecisionSource if self.decision else VerifiedDraftSource
        return cls(encoded(dict(credential_hash='a'*64,key_id='fixture',material={},proof='b'*64)))


def setup():
    repo=Repository();drafts=Source();decisions=Source(True)
    return repo,drafts,decisions,ActionService(repo,HEADERS['origin'],drafts,decisions,clock=lambda:100)


def decision():return dict(expected_version=1,revision=1,stage=1,decision='approve',reason='',key=uid())


@pytest.mark.parametrize('change',[
    dict(expected_version=True),dict(revision=0),dict(stage=11),dict(decision='pay'),
    dict(decision='reject',reason='  '),dict(reason='x'*4097),dict(reason='nul\x00'),dict(key='not-uuid'),
])
def test_decision_rejects_invalid_or_unbound_human_material_before_any_repository_call(change):
    repo,_,_,service=setup()
    with pytest.raises(ActionFailure) as error:asyncio.run(service.decide('s'*43,HEADERS,uid(),{**decision(),**change}))
    assert error.value.status==400 and not repo.calls


def test_confirmation_retry_uses_same_server_operation_and_receipt_ids():
    repo,drafts,_,service=setup();values=dict(expected_version=2,material_hash='a'*64,key=uid())
    asyncio.run(service.confirm('s'*43,HEADERS,repo.draft['id'],values));first=repo.calls[-1][2]
    asyncio.run(service.confirm('s'*43,HEADERS,repo.draft['id'],values));second=repo.calls[-1][2]
    assert first['operation_id']==second['operation_id'] and first['receipt_id']==second['receipt_id']
    assert first['source'] is not None and len(drafts.calls)==2


@pytest.mark.parametrize('kind',['confirm','decision'])
def test_already_durable_intent_replay_does_not_depend_on_source_availability(kind):
    repo,drafts,decisions,service=setup();repo.replay=True
    values=dict(expected_version=2,material_hash='a'*64,key=uid()) if kind=='confirm' else decision()
    result=asyncio.run(getattr(service,'confirm' if kind=='confirm' else 'decide')('s'*43,HEADERS,repo.draft['id'],values))
    assert result['state']=='queued' and not drafts.calls and not decisions.calls and len(repo.calls)==1


def test_current_authority_after_source_read_can_reject_confirmation_without_success():
    repo,drafts,_,service=setup();drafts.hook=lambda:setattr(repo,'fail',True)
    with pytest.raises(ActionFailure) as error:asyncio.run(service.confirm('s'*43,HEADERS,repo.draft['id'],dict(expected_version=2,material_hash='a'*64,key=uid())))
    assert error.value.status==409 and len(repo.calls)==2


def test_expiry_after_commit_discards_private_result():
    repo,_,_,service=setup();repo.expiry=100
    with pytest.raises(ActionFailure) as error:asyncio.run(service.read('s'*43,uid()))
    assert error.value.status==401


def test_same_key_is_scoped_by_original_actor_tenant_and_action_kind():
    repo,_,_,_=setup();key=uid();ctx=repo.context
    a=identifiers(ctx,key,'confirm')
    assert a!=identifiers(ctx,key,'decision') and a!=identifiers({**ctx,'actor_id':uid()},key,'confirm')


def test_mutation_requires_origin_csrf_and_no_prevalidated_allow_fields():
    repo,_,_,service=setup()
    with pytest.raises(ActionFailure) as error:asyncio.run(service.decide('s'*43,{**HEADERS,'origin':'https://foreign.test'},uid(),decision()))
    assert error.value.status==403 and not repo.calls
    with pytest.raises(ActionFailure) as error:asyncio.run(service.decide('s'*43,HEADERS,uid(),{**decision(),'allow':True}))
    assert error.value.status==400 and not repo.calls


@pytest.mark.parametrize('raw',[b'{}',b'{"proof":"a","proof":"b"}',b'{"material":NaN}',b'x'*16385])
def test_private_decision_evidence_rejects_ambiguous_or_oversized_material(raw):
    with pytest.raises(ActionFailure):VerifiedDecisionSource(raw).wire()
