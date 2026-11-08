from dataclasses import replace
import logging
from uuid import uuid4
import pytest
from reimbursement_service.config import Settings, ConfigurationError, load_settings
from reimbursement_service.composition import database_loop_factory
from reimbursement_service.infrastructure.database.target import (
    TargetError, parse_target, check_pair, schema_and_role, SafeDriverLog,
)
from reimbursement_service.infrastructure.database.connection import Limits, RuntimeDatabase
from reimbursement_service.modules.system.domain import ActorContext

URL = 'postgresql://reimbursement_runtime:fictional-password@db.example.test/service'


def test_service_env_does_not_adopt_platform_database():
    with pytest.raises(ConfigurationError):
        load_settings({'DATABASE_URL': URL})
    settings = load_settings({'REIMBURSEMENT_DATABASE_URL': URL})
    assert settings.schema == 'reimbursement'
    assert 'fictional-password' not in repr(settings)


@pytest.mark.parametrize('changes', [dict(schema='platform'), dict(schema='public'), dict(pool_size=3),
    dict(pool_size=True), dict(port=0), dict(host='arbitrary.test')])
def test_invalid_settings(changes):
    with pytest.raises(ConfigurationError):
        Settings(database_url=URL, **changes)


@pytest.mark.parametrize('url', [URL+'?sslmode=disable', URL+'?options=secret', URL+'?sslmode=require&sslmode=require',
    URL+'#fragment', URL.replace('@','%zz@'), URL.replace('db.example.test','db.example.test:0'),
    URL.replace('fictional-password',''), URL.replace('/service','/'), URL+'?hostaddr=1.2.3.4'])
def test_url_rejections_are_sanitized(url):
    with pytest.raises(TargetError) as caught:
        parse_target(url)
    assert 'fictional-password' not in str(caught.value)


def test_target_enforces_tls_and_does_not_leak_repr():
    target = parse_target(URL+'?sslmode=require')
    assert target.kwargs()['sslmode'] == 'verify-full'
    assert target.kwargs()['prepare_threshold'] is None
    assert 'fictional' not in repr(target)


def test_separate_service_login_and_same_database_pair():
    runtime = parse_target(URL)
    migration = replace(runtime, user='operator')
    check_pair(migration,runtime,'reimbursement')
    for invalid in [runtime, replace(migration, dbname='foreign')]:
        with pytest.raises(TargetError): check_pair(invalid,runtime,'reimbursement')
    with pytest.raises(TargetError): RuntimeDatabase(replace(runtime,user='platform_runtime'))
    assert schema_and_role('ref01_probe_'+'a'*32) == 'ref01_runtime_'+'a'*32


def test_internal_context_and_resource_bounds():
    assert ActorContext(uuid4(),uuid4()).tenant_id
    with pytest.raises(ValueError): ActorContext('tenant',uuid4())
    for invalid in [float('nan'), float('inf'), 0, True, 61]:
        with pytest.raises(ValueError): Limits(transaction_seconds=invalid)


def test_driver_log_redacts_error_and_input():
    record = logging.LogRecord('psycopg.pool',40,'',0,'password=%s',('secret',),None)
    record.exc_text = 'sensitive driver traceback'
    assert SafeDriverLog().filter(record)
    assert record.getMessage() == 'Service database driver event.' and record.exc_text is None


def test_database_loop_supports_windows_psycopg():
    import asyncio
    import sys
    loop=database_loop_factory()
    try:
        assert not loop.is_running()
        if sys.platform=='win32': assert isinstance(loop,asyncio.SelectorEventLoop)
    finally: loop.close()
