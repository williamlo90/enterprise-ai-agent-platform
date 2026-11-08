import asyncio
import secrets
from uuid import uuid4
import pytest
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.integrations.platform.proposed import ProposedClients
from reimbursement_service.modules.access.domain import AccessFailure,FailureKind


def test_proposed_clients_disabled_without_network():
    async def run():
        c=PlatformClient(PlatformTarget('https://fixture.test',secrets.token_urlsafe(32)))
        try:
            proposed=ProposedClients(c)
            for method,arguments in [('eligibility',dict(policy={},operation_kind='submission',trip_date='2026-11-05',claim_ref=None)),
                                     ('pin',dict(claim={},receipts=[])),('finalise',dict(claim={},outcome='committed',proof_digest='a'*64))]:
                with pytest.raises(AccessFailure) as caught:await getattr(proposed,method)(**arguments)
                assert caught.value.kind is FailureKind.UNAVAILABLE
        finally:await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind,claim',[('submission',str(uuid4())),('information',None),('decision',None),('decision','bad')])
def test_locked_claim_shape_rejected_before_transport(kind,claim):
    async def run():
        c=PlatformClient(PlatformTarget('https://fixture.test',secrets.token_urlsafe(32)))
        try:
            proposed=ProposedClients(c,fixture_contract_enabled=True)
            with pytest.raises(AccessFailure) as caught:
                await proposed.eligibility(policy=dict(id=str(uuid4()),version=1,digest='a'*64),
                    operation_kind=kind,trip_date='2026-11-05',claim_ref=claim)
            assert caught.value.kind is FailureKind.INVALID
        finally:await c.close()
    asyncio.run(run())
