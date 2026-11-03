import asyncio
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from enterprise_ai_platform import security_cli
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import DatabaseUnavailable, parse_target
from enterprise_ai_platform.modules.identity.domain import TenantActorContext
from test_database import database


def run(coroutine):
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:
        return runner.run(coroutine)


def test_context_requires_identifiers_and_hides_actor_data():
    tenant, actor = uuid4(), uuid4()
    context = TenantActorContext(tenant, actor)
    assert str(tenant) not in repr(context)
    assert str(actor) not in repr(context)
    with pytest.raises(FrozenInstanceError):
        context.actor_id = uuid4()
    for first, second in ((str(tenant), actor), (tenant, str(actor)), (None, actor)):
        with pytest.raises(ValueError, match="UUID identifiers"):
            TenantActorContext(first, second)


def test_context_is_transaction_local_and_missing_context_overwrites_it():
    async def check():
        db = database()
        first, second = TenantActorContext(uuid4(), uuid4()), TenantActorContext(uuid4(), uuid4())
        for context in (first, None, second):
            async with db.transaction(context):
                pass
        settings = [entry for entry in db.pool.connection.events
                    if isinstance(entry, tuple) and "set_config" in entry[0]]
        assert [params[-2:] for _, params in settings] == [(str(first.tenant_id), str(first.actor_id)), ("", ""), (str(second.tenant_id), str(second.actor_id))]
        assert all("app.tenant_id', %s, true" in query and "app.actor_id', %s, true" in query for query, _ in settings)
        assert "context" not in db.__dict__ and "actor_id" not in db.__dict__
        assert len(db.pool.returned) == 3
    run(check())


@pytest.mark.parametrize("context", ["synthetic-context", {}, (uuid4(), uuid4())])
def test_raw_context_is_rejected_before_checkout(context):
    async def check():
        db = database()
        with pytest.raises(ValueError, match="backend tenant/actor"):
            async with db.transaction(context):
                pytest.fail("Invalid context accepted")
        assert not db.pool.connection.events and not db.pool.returned
    run(check())


@pytest.mark.parametrize("result", [(True, False), (False, True), (False, False)])
def test_startup_rejects_privileged_unbound_or_changed_roles(result):
    async def check():
        db = database()
        db.pool.connection.role_valid = result
        with pytest.raises(DatabaseUnavailable):
            await db.open()
        assert db.pool.closed
    run(check())


def synthetic_env(path):
    path.write_text("APP_ENV=test\nAPP_HOST=127.0.0.1\nAPP_PORT=8000\nAPP_DATABASE_ENABLED=false\n"
                    "DATABASE_URL='postgres://original:synthetic-old@ep-test-pooler.example.test/db?sslmode=require'\n"
                    "DATABASE_DIRECT_URL='postgres://admin:synthetic-direct@ep-test.example.test/db?sslmode=require'\n", encoding="utf-8")


def test_provision_saves_only_runtime_url_without_printing_secret(monkeypatch, tmp_path, capsys):
    path = tmp_path / ".env"
    synthetic_env(path)
    before = load_settings(path, settings_type=DatabaseProbeSettings)
    async def provision(direct, base, **kwargs):
        return replace(base, user="enterprise_ai_platform_runtime", password="synthetic-new-secret")
    monkeypatch.setattr(security_cli, "provision_role", provision)
    monkeypatch.setattr(sys, "argv", ["security", "provision", "--env-file", str(path)])
    security_cli.main()
    after = load_settings(path, settings_type=DatabaseProbeSettings)
    assert after.database_direct_url == before.database_direct_url
    assert not after.database_enabled
    target = parse_target(after.database_url.get_secret_value())
    assert target.user == "enterprise_ai_platform_runtime" and target.password == "synthetic-new-secret"
    output = capsys.readouterr()
    assert "synthetic" not in output.out + output.err


@pytest.mark.parametrize("failure", ["file-write", "concurrent-edit", "environment-override"])
def test_provision_failure_does_not_overwrite_local_or_expose_secrets(monkeypatch, tmp_path, capsys, failure):
    path = tmp_path / ".env"
    synthetic_env(path)
    before = path.read_text(encoding="utf-8")
    calls = []
    async def provision(direct, base, **kwargs):
        calls.append(True)
        if failure == "concurrent-edit":
            path.write_text(before.replace("APP_PORT=8000", "APP_PORT=9000"), encoding="utf-8")
        return replace(base, user="enterprise_ai_platform_runtime", password="synthetic-new-secret")
    monkeypatch.setattr(security_cli, "provision_role", provision)
    if failure == "file-write":
        def failed_write(*args, **kwargs):
            raise OSError("synthetic-private-error")
        monkeypatch.setattr(security_cli, "set_key", failed_write)
    if failure == "environment-override":
        monkeypatch.setenv("DATABASE_URL", "synthetic-process-secret")
    monkeypatch.setattr(sys, "argv", ["security", "provision", "--env-file", str(path)])
    with pytest.raises(SystemExit) as exc:
        security_cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "synthetic" not in output.out + output.err
    assert "synthetic-new-secret" not in path.read_text(encoding="utf-8")
    if failure == "environment-override":
        assert not calls
    if failure != "concurrent-edit":
        assert path.read_text(encoding="utf-8") == before
    else:
        assert "APP_PORT=9000" in path.read_text(encoding="utf-8")


def test_provision_uri_preserves_endpoint_database_and_tls_options():
    original = "postgresql://old:old@ep-test-pooler.example.test:5432/db%20name?sslmode=require&channel_binding=require"
    updated = security_cli.runtime_url(original, "runtime", "synthetic:secret@/")
    result = parse_target(updated)
    assert result.password == "synthetic:secret@/"
    assert (result.host, result.port, result.dbname, result.channel_binding) == (
        "ep-test-pooler.example.test", 5432, "db name", "require")
