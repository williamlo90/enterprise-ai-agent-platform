import asyncio
from types import SimpleNamespace
import httpx
import pytest

from reimbursement_service.composition import claims_service
from reimbursement_service.modules.claims.domain import ClaimFailure
from reimbursement_service.modules.claims.service import ClaimsService,DenyClaimsAuthority
from reimbursement_service.infrastructure.database.claims import PostgresClaims
from reimbursement_service.infrastructure.database.operations import PostgresOperations
from reimbursement_service.config import Settings
from reimbursement_service.main import create_app


def test_actual_claim_factory_uses_one_database_and_default_deny_without_opening_resources():
    database=SimpleNamespace();proofs=SimpleNamespace()
    service=claims_service(database,proofs)
    assert isinstance(service,ClaimsService) and isinstance(service.repository,PostgresClaims)
    assert isinstance(service.operations.repository,PostgresOperations)
    assert service.repository.database is service.operations.repository.database is database
    assert service.operations.proofs is proofs and isinstance(service.authority,DenyClaimsAuthority)
    async def run():
        with pytest.raises(ClaimFailure) as error:await service.authority.authorize(None,None)
        assert error.value.status==503
    asyncio.run(run())


def test_foundation_does_not_automatically_mount_claim_writer():
    calls=[]
    class Database:
        async def open(self):calls.append('open')
        async def close(self):calls.append('close')
    app=create_app(Settings(database_url='fixture'),resource_factory=lambda _: (Database(),SimpleNamespace()))
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://fixture.test') as client:
                assert (await client.post('/service/v1/claims',json={'allow':True})).status_code==404
                assert (await client.post('/service/v1/claims/fixture/decisions',json={'approve':True})).status_code==404
    asyncio.run(run());assert calls==['open','close']
