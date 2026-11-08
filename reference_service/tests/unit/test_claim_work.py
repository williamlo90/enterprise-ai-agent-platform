import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from uuid import uuid4
import pytest

from reimbursement_service.modules.claims.domain import ClaimFailure
from reimbursement_service.modules.claims.service import ClaimWork
from reimbursement_service.modules.operations.domain import Result


def work(value=None,*,clock=lambda:100):
    result=Result(str(uuid4()),1,'claim_submitted')
    packet=json.dumps({'claim_id':result.ref,'expires_at':110,'private':'fictional-private-packet'}).encode()
    repository=SimpleNamespace(apply=AsyncMock(return_value=value if value is not None else {'status':'ok','result':result.wire()}))
    return ClaimWork(repository,'submit',packet,result,clock),repository


def test_same_unit_of_work_is_used_and_private_packet_is_not_in_repr():
    async def run():
        value,repo=work();uow=object()
        outcome,result=await value.apply(uow)
        assert outcome=='committed' and result==value.expected
        assert repo.apply.call_args.args[0] is uow and repo.apply.call_args.args[1]=='submit'
        assert 'fictional-private-packet' not in repr(value)
    asyncio.run(run())


@pytest.mark.parametrize('code',['business_duplicate','stale_claim','stale_stage','deadline_passed','terminal_claim','claim_exists','invalid_previous_claim','stale_draft'])
def test_authoritative_business_conflict_becomes_rejected_ledger_result(code):
    async def run():
        value,_=work({'status':'conflict','code':code})
        state,result=await value.apply(object())
        assert state=='rejected' and result.wire()=={'ref':None,'version':None,'code':code}
    asyncio.run(run())


@pytest.mark.parametrize('response,status',[(None,503),({'status':'conflict','code':'private error body'},503),({'status':'ok','result':{}},503),
    ({'status':'invalid'},400),({'status':'unauthenticated'},401),({'status':'forbidden'},403),({'status':'missing'},404),({'status':'unknown'},503)])
def test_untrusted_result_never_becomes_business_success(response,status):
    async def run():
        value,repo=work();repo.apply.return_value=response
        with pytest.raises(ClaimFailure) as error:await value.apply(object())
        assert error.value.status==status and 'private error body' not in str(error.value)
    asyncio.run(run())


def test_expired_authority_prevents_any_write():
    async def run():
        value,repo=work(clock=lambda:110)
        with pytest.raises(ClaimFailure) as error:await value.apply(object())
        assert error.value.status==401;repo.apply.assert_not_called()
    asyncio.run(run())


def test_expiry_after_sql_effects_is_propagated_to_caller_for_transaction_rollback():
    async def run():
        value,repo=work(clock=Mock(side_effect=[100,110]))
        with pytest.raises(ClaimFailure) as error:await value.apply(object())
        assert error.value.status==401 and repo.apply.await_count==1
    asyncio.run(run())


@pytest.mark.parametrize('packet',[b'{"claim_id":"x","claim_id":"y"}',b'{"expires_at":NaN}',b'[]'])
def test_ambiguous_internal_packets_fail_closed(packet):
    with pytest.raises(ClaimFailure):ClaimWork(SimpleNamespace(),'submit',packet,Result(str(uuid4()),1,'claim_submitted'))
