import asyncio
from types import SimpleNamespace
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
import pytest
from enterprise_ai_platform.config import Settings,ConfigurationError
from enterprise_ai_platform import composition
from enterprise_ai_platform.delegation_check import SigningFixture


def values():
    private=SigningFixture().private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    issuer='https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test'
    domain='https://fixture.auth.ap-southeast-1.amazoncognito.com'
    return dict(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url='postgresql://runtime:fixture@db.example.test/platform',
        auth_enabled=True,auth_callback_logging_controlled=True,auth_jwks_background_refresh=False,auth_verifier_key=Fernet.generate_key().decode(),
        oidc_issuer=issuer,oidc_client_id='synthetic-client',oidc_client_secret='private-fixture',public_origin='https://app.example.test',
        oidc_redirect_uri='https://app.example.test/api/v1/auth/callback',oidc_authorization_endpoint=domain+'/oauth2/authorize',
        oidc_token_endpoint=domain+'/oauth2/token',oidc_jwks_uri=issuer+'/.well-known/jwks.json',internal_auth_enabled=True,
        knowledge_enabled=True,browser_context_enabled=True,reimbursement_enabled=True,reimbursement_origin='https://reference.example.test',
        reimbursement_dispatcher_credential='r'*43,reimbursement_signing_private_key=private)


@pytest.mark.parametrize('changes',[dict(database_enabled=False),dict(auth_enabled=False),dict(internal_auth_enabled=False),dict(knowledge_enabled=False),
    dict(browser_context_enabled=False),dict(reimbursement_origin='http://reference.test'),dict(reimbursement_origin='https://user:secret@reference.test'),
    dict(reimbursement_origin='https://reference.test/path'),dict(reimbursement_dispatcher_credential='short'),dict(reimbursement_signing_private_key=None),
    dict(reimbursement_signing_kid='bad key'),dict(reimbursement_settlement_key_id='bad key'),dict(reimbursement_enabled='yes')])
def test_incomplete_or_unsafe_integration_config_cannot_enable(changes):
    with pytest.raises(ValueError):Settings(**{**values(),**changes})


def test_bad_private_key_rejected_before_allocating_database_or_network_clients(monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('invalid private key allocated external resources')
    monkeypatch.setattr(composition,'RuntimeDatabase',forbidden)
    monkeypatch.setattr(composition,'PolicyReceiver',forbidden)
    with pytest.raises(ConfigurationError) as error:composition.build_resources(Settings(**{**values(),'reimbursement_signing_private_key':'private-invalid-key-marker'}))
    assert 'private-invalid-key-marker' not in str(error.value)


def test_flag_off_does_not_construct_signer_or_reimbursement_clients(monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('disabled feature allocated integration')
    monkeypatch.setattr(composition,'PolicyReceiver',forbidden);monkeypatch.setattr(composition,'TripCatalogReader',forbidden)
    resources=composition.build_resources(Settings(env='test',host='127.0.0.1',port=8000))
    services=composition.build_services(resources)
    assert services.policies is None and services.trips is None and resources.reimbursement_clients==()


def test_complete_opt_in_builds_real_adapters_and_lifecycle_closes_them_without_network(monkeypatch):
    calls=[]
    class Database:
        def __init__(self,*args,**kwargs):calls.append('constructed')
        async def open(self):calls.append('opened')
        async def close(self):calls.append('closed')
    monkeypatch.setattr(composition,'RuntimeDatabase',Database)
    settings=Settings(**values());resources=composition.build_resources(settings);services=composition.build_services(resources)
    assert services.policies is resources.policies and services.trips is resources.trips
    assert len(resources.reimbursement_clients)==2
    assert resources.delegation.receipts is None  # ACT-01 issuance remains a distinct producer.
    assert 'r'*43 not in repr(settings) and 'PRIVATE KEY' not in repr(settings)
    async def run():
        async with resources.lifecycle():assert resources.readiness.ready and calls==['constructed','opened']
        assert not resources.readiness.ready
        assert all(client.http.is_closed for client in resources.reimbursement_clients)
    asyncio.run(run());assert calls==['constructed','opened','closed']


def test_failed_http_client_close_still_attempts_other_client_storage_and_database_cleanup():
    calls=[]
    class Resource:
        def __init__(self,name,fail=False):self.name,self.fail=name,fail
        async def open(self):pass
        async def close(self):
            calls.append(self.name)
            if self.fail:raise RuntimeError('controlled-close-failure')
    resources=composition.ApplicationResources(readiness=SimpleNamespace(ready=False),database=Resource('database'),storage=Resource('storage'),
        reimbursement_clients=(Resource('receiver'),Resource('reader',True)))
    async def run():
        with pytest.raises(RuntimeError,match='controlled-close-failure'):
            async with resources.lifecycle():pass
        assert not resources.readiness.ready
    asyncio.run(run());assert calls==['reader','receiver','storage','database']


def test_actual_internal_factory_mounts_private_policy_with_real_services_and_deny_defaults(monkeypatch):
    import httpx
    from enterprise_ai_platform.internal_api import create_internal_app
    from enterprise_ai_platform.infrastructure.database.policies import PostgresPolicyRepository
    from enterprise_ai_platform.modules.policies.service import PolicyService, DenyLockedBindings
    from enterprise_ai_platform.modules.delegation.service import DelegationService
    calls=[]
    class OwnedDatabase:
        def __init__(self,*args,**kwargs):calls.append('constructed')
        async def open(self):calls.append('opened')
        async def close(self):calls.append('closed')
    monkeypatch.setattr(composition,'RuntimeDatabase',OwnedDatabase)
    # No injected ApplicationServices or fake policy/delegation repositories.
    app=create_internal_app(Settings(**values()))
    resource=app.state.resources;service=app.state.services.policies
    assert isinstance(service,PolicyService) and service is resource.policies
    assert isinstance(service.repository,PostgresPolicyRepository)
    assert service.repository.database is resource.database
    assert isinstance(service.delegation,DelegationService) and service.delegation is resource.delegation
    assert service.delegation.repository.database is resource.database
    assert service.delegation.receipts is None and isinstance(service.locked,DenyLockedBindings)
    async def run():
        async with app.router.lifespan_context(app):
            assert resource.readiness.ready
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://internal.example.test') as client:
                response=await client.post('/internal/v1/policy-packages/00000000-0000-0000-0000-000000000001/eligibility',json={})
                assert response.status_code==401 and response.headers['cache-control']=='no-store'
                # Exercise actual routing (FastAPI may hold lazy included routers).
                assert (await client.post('/api/v1/policy-packages/00000000-0000-0000-0000-000000000001/versions/1/prepare',json={})).status_code==404
                assert (await client.get('/api/v1/trips')).status_code==404
        assert not resource.readiness.ready
        assert all(c.http.is_closed for c in resource.reimbursement_clients)
    asyncio.run(run())
    assert calls==['constructed','opened','closed']
