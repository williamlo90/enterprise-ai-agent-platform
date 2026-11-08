"""Draft material invariants and authority across the external source await."""
import asyncio
import copy
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.drafts.domain import editable,material_hash,source_trip,VerifiedDraftSource,DraftFailure,encoded
from enterprise_ai_platform.modules.drafts.service import DraftService
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure


def uid():return str(uuid4())


def content():
    receipt=uid()
    return dict(currency='SGD',items=[dict(id=uid(),trip_item_id=uid(),amount_minor=500,receipt_ids=[receipt])],receipts=[dict(id=receipt,version=1)])


@pytest.mark.parametrize('mutation',[
    lambda c:c['items'][0].update(amount_minor=True),
    lambda c:c['items'][0].update(amount_minor=0),
    lambda c:c['items'][0].update(amount_minor=9223372036854775808),
    lambda c:c['items'].append(copy.deepcopy(c['items'][0])),
    lambda c:c['items'][0]['receipt_ids'].append(c['items'][0]['receipt_ids'][0]),
    lambda c:c['receipts'][0].update(version=True),
    lambda c:c['receipts'].clear(),
    lambda c:c.update(approved=True),
])
def test_structural_content_rejects_invalid_financial_or_receipt_material(mutation):
    value=content();mutation(value)
    with pytest.raises(ValueError):editable(value)


def test_empty_draft_can_be_saved_without_claiming_preview_readiness():
    assert editable(dict(currency='SGD',items=[],receipts=[]))==dict(currency='SGD',items=[],receipts=[])


def test_order_is_canonical_but_financial_changes_alter_material_hash():
    a=content();b=content();a['items']+=b['items'];a['receipts']+=b['receipts']
    reversed_value=dict(currency='SGD',items=list(reversed(a['items'])),receipts=list(reversed(a['receipts'])))
    assert editable(a)==editable(reversed_value)
    original=material_hash(editable(a));a['items'][0]['amount_minor']+=1
    assert material_hash(editable(a))!=original


def test_source_allows_authoritative_booking_before_travel():
    trip=uid();value=dict(id=trip,start_date='2026-11-05',end_date='2026-11-06',state='completed',employee_ref='Fictional',
        items=[dict(id=uid(),expense_date='2026-10-01',category='travel',declared_amount_minor=500,currency='SGD')])
    assert source_trip(value,trip)==value
    value['items'].append(copy.deepcopy(value['items'][0]))
    with pytest.raises(ValueError):source_trip(value,trip)


@pytest.mark.parametrize('raw',[b'{"proof":"a","proof":"b"}',b'{"proof":NaN}',b'{}',b'x'*65537])
def test_private_evidence_rejects_ambiguous_or_oversized_json(raw):
    with pytest.raises(DraftFailure):VerifiedDraftSource(raw).wire()


class Repository:
    def __init__(self):
        self.calls=[];self.expiry=200;self.replay=False;self.fail=False
        self.context=dict(tenant_id=uid(),actor_id=uid(),membership_id=uid(),session_id=uid(),access_revision=1,authority_expires_at=200)
        self.draft=dict(id=uid(),trip_id=uid(),claim_id=None,state='editable',version=1)
    async def call(self,token,operation,payload):
        self.calls.append((token,operation,payload))
        if self.fail:raise DraftFailure(409)
        return dict(authority_expires_at=self.expiry,context=self.context,draft=None if operation=='create' and payload['source'] is None and not self.replay else self.draft,preview=dict(preview=True))


class Source:
    def __init__(self,repository):self.repository=repository;self.calls=[];self.hook=lambda:None
    async def read(self,context,trip,claim):
        self.calls.append((context,trip,claim));self.hook()
        return VerifiedDraftSource(encoded(dict(credential_hash='a'*64,key_id='fixture',material={},proof='b'*64)))


HEADERS={'origin':'https://app.example.test','content-type':'application/json','x-csrf-token':'c'*43}


def test_create_replay_avoids_external_reads_and_deterministic_candidate_survives_retry():
    repo=Repository();source=Source(repo);service=DraftService(repo,HEADERS['origin'],source,clock=lambda:100)
    values=dict(kind='submission',trip_id=uid(),claim_id=None,conversation_id=None,key=uid())
    asyncio.run(service.create('s'*43,HEADERS,values));candidate=repo.calls[-1][2]['id']
    asyncio.run(service.create('s'*43,HEADERS,values));assert repo.calls[-1][2]['id']==candidate
    repo.replay=True;count=len(source.calls)
    asyncio.run(service.create('s'*43,HEADERS,values));assert len(source.calls)==count


def test_external_source_await_does_not_replace_current_repository_authority():
    repo=Repository();source=Source(repo);service=DraftService(repo,HEADERS['origin'],source,clock=lambda:100)
    source.hook=lambda:setattr(repo,'fail',True)
    with pytest.raises(DraftFailure) as error:asyncio.run(service.preview('s'*43,HEADERS,repo.draft['id'],dict(expected_version=1)))
    assert error.value.status==409 and [c[1] for c in repo.calls]==['read','preview']


def test_expiry_after_repository_await_discards_response():
    repo=Repository();repo.expiry=100;service=DraftService(repo,HEADERS['origin'],clock=lambda:100)
    with pytest.raises(DraftFailure) as error:asyncio.run(service.read('s'*43,repo.draft['id']))
    assert error.value.status==401


def test_stale_preview_is_rejected_before_external_source_read():
    repo=Repository();source=Source(repo);service=DraftService(repo,HEADERS['origin'],source,clock=lambda:100)
    with pytest.raises(DraftFailure) as error:asyncio.run(service.preview('s'*43,HEADERS,repo.draft['id'],dict(expected_version=2)))
    assert error.value.status==409 and not source.calls


def test_mutation_requires_origin_and_csrf_before_source_authority():
    repo=Repository();source=Source(repo);service=DraftService(repo,HEADERS['origin'],source,clock=lambda:100)
    with pytest.raises(DraftFailure) as error:asyncio.run(service.preview('s'*43,{**HEADERS,'origin':'https://foreign.example.test'},repo.draft['id'],dict(expected_version=1)))
    assert error.value.status==403 and not repo.calls and not source.calls
