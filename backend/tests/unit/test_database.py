"""Offline failure injection; these tests do not certify a live database."""

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from enterprise_ai_platform.composition import ApplicationResources, database_loop_factory
from enterprise_ai_platform.config import ConfigurationError, DatabaseProbeSettings, Settings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import (
    DatabaseConfigurationError, DatabaseLimits, DatabaseUnavailable, RuntimeDatabase,
    install_safe_database_logging, parse_target, verify_same_database,
    direct_connection,
)
from enterprise_ai_platform.infrastructure.runtime import RuntimeReadiness
from enterprise_ai_platform.main import create_app

URI = "postgresql://runtime:synthetic-secret@ep-test-pooler.example.test/testdb?sslmode=require"
DIRECT = "postgresql://migrator:synthetic-direct@ep-test.example.test/testdb?sslmode=verify-full"


def run(coroutine):
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:
        return runner.run(coroutine)


class FakeConnection:
    def __init__(self):
        self.events = []
        self.closed = False
        self.rows = (1,)
        self.pgconn = SimpleNamespace(ssl_in_use=True)
        self.exit_delay = 0
        self.last_query = ""
        self.role_valid = (True, True)

    @asynccontextmanager
    async def transaction(self):
        self.events.append("begin")
        try:
            yield self
        except BaseException:
            self.events.append("rollback")
            await asyncio.sleep(self.exit_delay)
            raise
        else:
            self.events.append("commit")
            await asyncio.sleep(self.exit_delay)

    async def execute(self, query, params=None):
        self.events.append((query, params))
        self.last_query = query if isinstance(query, str) else query.as_string()
        return self

    async def fetchone(self):
        return self.role_valid if "runtime_role_is_valid" in self.last_query else self.rows

    async def close(self):
        self.closed = True


class FakePool:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connection = FakeConnection()
        self.returned = []
        self.closed = False
        self.checkout_delay = 0
        self.return_delay = 0

    async def open(self):
        pass

    async def close(self, timeout):
        self.closed = True

    async def getconn(self, timeout):
        await asyncio.sleep(self.checkout_delay)
        return self.connection

    async def putconn(self, connection):
        await asyncio.sleep(self.return_delay)
        self.returned.append(connection)


def database(**limits):
    return RuntimeDatabase(parse_target(URI), limits=DatabaseLimits(**limits), pool_factory=FakePool)


@pytest.mark.parametrize("value", [
    "", "not-a-uri", "http://user:secret@host/db", "postgres://host/db",
    "postgres://user@host/db", "postgres://user:secret@host/",
    "postgres://user:secret@host/db#fragment", "postgres://user:secret@host:99999/db",
    "postgres://user:secret@host:0/db",
    "postgres://user:secret@host/db?sslmode=disable", "postgres://user:secret@host/db?sslmode=verify-ca",
    "postgres://user:secret@host/db?host=other", "postgres://user:secret@host/db?hostaddr=127.0.0.1",
    "postgres://user:secret@host/db?options=-cfoo", "postgres://user:secret@host/db?service=foo",
    "postgres://user:secret@host/db?sslmode=require&sslmode=disable",
    "postgres://user:secret@host/db?channel_binding=disable", "postgres://user:sec%00ret@host/db",
    "postgres://user:sec%xxret@host/db", "postgres://user:secret@host,other/db",
    "postgres://user:secret@host/db\n", "postgres://user:secret@host/db?sslrootcert=evil",
])
def test_database_uri_rejects_unsafe_or_invalid_values_without_echo(value):
    with pytest.raises(DatabaseConfigurationError) as exc:
        parse_target(value)
    assert str(exc.value) == "Missing or invalid database configuration."
    assert "secret" not in repr(exc.value)


def test_tls_upgrade_secret_repr_and_explicit_connection_parameters():
    target = parse_target(URI)
    assert repr(target) == "ConnectionTarget(<redacted>)"
    kwargs = target.kwargs()
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["sslrootcert"]
    assert kwargs["prepare_threshold"] is None
    assert kwargs["autocommit"] is True
    assert kwargs["connect_timeout"] == 5
    assert kwargs["gssencmode"] == "disable"
    assert parse_target("postgres://user:p%40ss@host/db").password == "p@ss"
    db = database()
    assert db.pool.kwargs["min_size"] == 0
    assert db.pool.kwargs["max_size"] == 2
    assert db.pool.kwargs["max_waiting"] == 8
    assert db.pool.kwargs["open"] is False
    assert db.pool.kwargs["check"] is not None


def test_direct_and_runtime_target_pairing():
    verify_same_database(parse_target(URI), parse_target(DIRECT, direct=True))
    with pytest.raises(DatabaseConfigurationError):
        parse_target(URI, direct=True)
    for value in (DIRECT.replace("testdb", "other"), DIRECT.replace("ep-test.", "ep-other.")):
        with pytest.raises(DatabaseConfigurationError):
            verify_same_database(parse_target(URI), parse_target(value, direct=True))


def test_api_does_not_load_migration_secret_and_disabled_database_needs_no_url(tmp_path):
    file = tmp_path / ".env"
    file.write_text("APP_ENV=test\nAPP_HOST=127.0.0.1\nAPP_PORT=8000\n"
                    "DATABASE_DIRECT_URL=synthetic-direct\nDATABASE_URL=\n", encoding="utf-8")
    settings = load_settings(file)
    assert not hasattr(settings, "database_direct_url")
    assert "synthetic" not in repr(settings)
    assert create_app(settings).state.resources.database is None
    with pytest.raises(DatabaseConfigurationError):
        create_app(settings.model_copy(update={"database_enabled": True}))
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        create_app(Settings(env="test", host="127.0.0.1", port=8000, database_enabled=True))


def test_database_config_env_precedence_and_sanitized_missing_fields(monkeypatch, tmp_path):
    file = tmp_path / ".env"
    file.write_text(f"APP_ENV=test\nAPP_HOST=127.0.0.1\nAPP_PORT=8000\nDATABASE_URL={URI}\n"
                    f"DATABASE_DIRECT_URL={DIRECT}\n", encoding="utf-8")
    probe = load_settings(file, settings_type=DatabaseProbeSettings)
    assert probe.database_direct_url.get_secret_value() == DIRECT
    monkeypatch.setenv("DATABASE_URL", "")
    assert load_settings(file).database_url.get_secret_value() == ""
    monkeypatch.setenv("APP_DATABASE_ENABLED", "synthetic-secret")
    with pytest.raises(ConfigurationError) as exc:
        load_settings(file)
    assert "APP_DATABASE_ENABLED" in str(exc.value)
    assert "synthetic-secret" not in str(exc.value)


def test_success_commits_and_exception_rolls_back_and_returns_connection():
    async def check():
        db = database()
        async with db.transaction() as connection:
            assert connection is db.pool.connection
        assert connection.events[-1] == "commit"
        settings_query, values = connection.events[1]
        assert "true" in settings_query
        assert values == ("5000", "2000", "10000", "", "")
        with pytest.raises(ValueError, match="business failure"):
            async with db.transaction():
                raise ValueError("business failure")
        assert connection.events[-1] == "rollback"
        assert db.pool.returned == [connection, connection]
        assert not connection.closed
    run(check())


def test_sql_error_is_sanitized_connection_discarded_and_pool_can_reuse():
    async def check():
        db = database()
        with pytest.raises(DatabaseUnavailable) as exc:
            async with db.transaction():
                raise psycopg.OperationalError("synthetic-secret SQL host user")
        assert "synthetic" not in str(exc.value)
        assert db.pool.connection.closed
        assert len(db.pool.returned) == 1
        db.pool.connection = FakeConnection()  # Simulates pool replacement.
        assert await db.ping()
        assert len(db.pool.returned) == 2
    run(check())


def test_cancellation_rolls_back_discards_and_returns_before_task_finishes():
    async def check():
        db = database()
        entered = asyncio.Event()
        async def work():
            async with db.transaction():
                entered.set()
                await asyncio.sleep(10)
        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert db.pool.connection.events[-1] == "rollback"
        assert db.pool.connection.closed
        assert db.pool.returned == [db.pool.connection]
    run(check())


@pytest.mark.parametrize("phase", ["return", "direct", "shutdown"])
def test_repeated_cancellation_cannot_interrupt_bounded_cleanup(monkeypatch, phase):
    from enterprise_ai_platform.infrastructure.database import connection as adapter
    async def check():
        started = asyncio.Event()
        completed = asyncio.Event()
        async def delayed_cleanup(*args, **kwargs):
            started.set()
            await asyncio.sleep(0.05)
            completed.set()
        db = database()
        if phase == "return":
            db.pool.putconn = delayed_cleanup
            async def work():
                async with db.transaction():
                    pass
        elif phase == "shutdown":
            db.pool.close = delayed_cleanup
            work = db.close
        else:
            fake = FakeConnection()
            fake.close = delayed_cleanup
            async def connect(**kwargs):
                return fake
            monkeypatch.setattr(adapter.psycopg.AsyncConnection, "connect", connect)
            async def work():
                async with direct_connection(parse_target(DIRECT, direct=True)):
                    pass
        task = asyncio.create_task(work())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()
    run(check())


@pytest.mark.parametrize("phase", ["checkout", "work", "rollback", "commit", "return"])
def test_deadlines_bound_checkout_work_transaction_exit_and_return(phase):
    async def check():
        db = database(checkout_seconds=0.02, transaction_seconds=0.05, cleanup_seconds=0.02)
        if phase == "checkout":
            db.pool.checkout_delay = 10
        if phase in {"rollback", "commit"}:
            db.pool.connection.exit_delay = 10
        if phase == "return":
            db.pool.return_delay = 10
        async with asyncio.timeout(1):
            with pytest.raises(DatabaseUnavailable):
                async with db.transaction():
                    if phase == "work":
                        await asyncio.sleep(10)
                    if phase == "rollback":
                        raise ValueError("synthetic rollback")
        if phase == "checkout":
            assert not db.pool.returned
        elif phase == "return":
            assert db.pool.closed
            assert db.pool.connection.closed
        else:
            assert db.pool.connection.closed
            assert db.pool.returned == [db.pool.connection]
    run(check())


def test_startup_requires_real_tls_probe_and_closes_on_failure():
    async def check():
        db = database()
        db.pool.connection.pgconn.ssl_in_use = False
        with pytest.raises(DatabaseUnavailable):
            await db.open()
        assert db.pool.closed
        db = database(checkout_seconds=0.02)
        db.pool.checkout_delay = 10
        async with asyncio.timeout(1):
            with pytest.raises(DatabaseUnavailable):
                await db.open()
        assert db.pool.closed
    run(check())


def test_failed_connection_close_does_not_skip_pool_shutdown():
    async def check():
        db = database(cleanup_seconds=0.02)
        db.pool.return_delay = 10
        async def broken_close():
            raise psycopg.OperationalError("synthetic-secret")
        db.pool.connection.close = broken_close
        with pytest.raises(DatabaseUnavailable):
            async with db.transaction():
                pass
        assert db.pool.closed
    run(check())


def test_resources_close_on_startup_failure_and_readiness_uses_database():
    class Dependency:
        healthy = True
        closed = False
        fail_start = False
        async def open(self):
            if self.fail_start:
                raise DatabaseUnavailable()
        async def close(self):
            self.closed = True
        async def ping(self):
            return self.healthy
    async def check():
        db = Dependency()
        resources = ApplicationResources(RuntimeReadiness(database=db), db)
        assert not await resources.readiness.check()
        async with resources.lifecycle():
            assert await resources.readiness.check()
            db.healthy = False
            assert not await resources.readiness.check()
        assert db.closed
        assert not resources.readiness.ready
        db.closed = False
        db.fail_start = True
        with pytest.raises(DatabaseUnavailable):
            async with resources.lifecycle():
                pytest.fail("Startup must fail closed")
        assert db.closed
        assert not resources.readiness.ready
    run(check())


def test_database_outage_affects_ready_but_not_live():
    app = create_app(Settings(env="test", host="127.0.0.1", port=8000))
    class Outage:
        async def ping(self):
            return False
    with TestClient(app) as client:
        app.state.resources.readiness.database = Outage()
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health/live").status_code == 200


def test_driver_logs_remove_raw_details_and_exception_trace(caplog):
    install_safe_database_logging()
    with caplog.at_level(logging.WARNING):
        try:
            raise ValueError("synthetic-secret")
        except ValueError:
            for name in ("psycopg", "psycopg.pool", "psycopg.transaction"):
                logging.getLogger(name).exception("host password=%s", "synthetic-secret")
    assert "synthetic" not in caplog.text
    assert "Traceback" not in caplog.text
    assert caplog.text.count("Database driver event.") == 3


def test_probe_cli_missing_credentials_is_safe_and_never_connects():
    result = subprocess.run([sys.executable, "-m", "enterprise_ai_platform.database_check", "--no-env-file"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "Database configuration missing or invalid.\n"


def test_probe_cli_validation_only_does_not_connect():
    environment = dict(os.environ, APP_ENV="test", APP_HOST="127.0.0.1", APP_PORT="8000",
                       DATABASE_URL=URI, DATABASE_DIRECT_URL=DIRECT)
    result = subprocess.run([sys.executable, "-m", "enterprise_ai_platform.database_check", "--no-env-file"],
                            env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout == "Database configuration valid. No database connection made.\n"
    assert result.stderr == ""


@pytest.mark.parametrize("failure", ["tls", "work", "cancel"])
def test_direct_connection_closes_on_tls_error_work_error_and_cancellation(monkeypatch, failure):
    from enterprise_ai_platform.infrastructure.database import connection as adapter
    fake = FakeConnection()
    if failure == "tls":
        fake.pgconn.ssl_in_use = False
    async def connect(**kwargs):
        assert kwargs["sslmode"] == "verify-full"
        assert kwargs["prepare_threshold"] is None
        return fake
    monkeypatch.setattr(adapter.psycopg.AsyncConnection, "connect", connect)
    async def check():
        expected = {"tls": DatabaseUnavailable, "work": ValueError, "cancel": asyncio.CancelledError}[failure]
        with pytest.raises(expected):
            async with direct_connection(parse_target(DIRECT, direct=True)):
                if failure == "work":
                    raise ValueError("Synthetic work error")
                if failure == "cancel":
                    raise asyncio.CancelledError()
        assert fake.closed
    run(check())


def test_windows_database_loop_is_selector():
    loop = database_loop_factory()
    try:
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
