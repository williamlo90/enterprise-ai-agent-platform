import asyncio
from types import SimpleNamespace
from uuid import uuid4
import pytest
import httpx
from reimbursement_service.config import Settings,ConfigurationError,load_settings
from reimbursement_service.main import create_app


def values():return dict(database_url='fixture',catalog_enabled=True,platform_origin='https://platform.example.test',platform_credential='p'*43,dispatcher_credential='d'*43)


@pytest.mark.parametrize('changes',[dict(platform_origin='http://platform.test'),dict(platform_origin='https://user:secret@platform.test'),
    dict(platform_origin='https://platform.test/path'),dict(platform_origin='https://platform.test?query=1'),dict(platform_origin='https://platform test'),
    dict(platform_credential='short'),dict(dispatcher_credential='short'),dict(dispatcher_credential='p'*43),dict(catalog_enabled='true')])
def test_incomplete_unsafe_or_same_credential_config_rejected(changes):
    with pytest.raises(ConfigurationError):Settings(**{**values(),**changes})


def test_env_flag_is_explicit_and_private_settings_are_redacted():
    assert not load_settings(dict(REIMBURSEMENT_DATABASE_URL='fixture')).catalog_enabled
    with pytest.raises(ConfigurationError):load_settings(dict(REIMBURSEMENT_DATABASE_URL='fixture',REIMBURSEMENT_CATALOG_ENABLED='yes'))
    text=repr(Settings(**values()))
    assert 'p'*43 not in text and 'd'*43 not in text


def test_opt_in_composes_actual_authority_clients_and_receiver_routes_and_closes_lifecycle():
    calls=[]
    class Database:
        async def open(self):calls.append('open')
        async def close(self):calls.append('close')
    database=Database()
    app=create_app(Settings(**values()),resource_factory=lambda settings:(database,SimpleNamespace()))
    catalog=app.state.catalog_resources
    assert catalog is not None and catalog.client.target.credential=='p'*43
    assert catalog.packages.scopes.client is catalog.client and catalog.trips.scopes.client is catalog.client
    assert catalog.eligibility.client is catalog.client
    async def run():
        async with app.router.lifespan_context(app):
            assert calls==['open']
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://reference.test') as client:
                assert (await client.get('/service/v1/trips',headers={'X-EAI-Operation-ID':str(uuid4())})).status_code==401
        assert catalog.client.http.is_closed
    asyncio.run(run());assert calls==['open','close']


def test_startup_failure_still_closes_http_client_and_database():
    calls=[]
    class Database:
        async def open(self):raise RuntimeError('controlled-startup-failure')
        async def close(self):calls.append('close')
    app=create_app(Settings(**values()),resource_factory=lambda settings:(Database(),SimpleNamespace()))
    async def run():
        with pytest.raises(RuntimeError,match='controlled-startup-failure'):
            async with app.router.lifespan_context(app):pytest.fail('startup did not fail')
        assert app.state.catalog_resources.client.http.is_closed
    asyncio.run(run());assert calls==['close']


def test_production_catalog_flag_cannot_be_combined_with_injected_authority_services():
    with pytest.raises(ConfigurationError):create_app(Settings(**values()),trips=SimpleNamespace())


@pytest.mark.parametrize('mode',['partial_operations','equal_credentials','partial_trip_services'])
def test_invalid_injected_combinations_rejected_before_any_resource_allocation(monkeypatch,mode):
    import reimbursement_service.main as main_module
    def forbidden(*args,**kwargs):pytest.fail('invalid combination allocated resources')
    monkeypatch.setattr(main_module,'catalog_resources',forbidden)
    injected=dict(operations=SimpleNamespace())
    if mode=='equal_credentials':
        authority=SimpleNamespace(dispatcher=SimpleNamespace(credential_digest='same'))
        injected.update(dispatch=authority,recovery=authority)
    if mode=='partial_trip_services':injected=dict(trips=SimpleNamespace())
    settings=Settings(**{**values(),'catalog_enabled':mode!='partial_trip_services'})
    with pytest.raises(ConfigurationError):create_app(settings,resource_factory=forbidden,**injected)
