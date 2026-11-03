import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database import roles
from enterprise_ai_platform.infrastructure.database.connection import parse_target

DIRECT = parse_target("postgres://admin:synthetic-direct@host/db")
BASE = parse_target("postgres://enterprise_ai_platform_runtime:synthetic-existing@host/db")


class Cursor:
    def __init__(self, row):
        self.row = row
    async def fetchone(self):
        return self.row


class Connection:
    def __init__(self, binding, oid, valid=True):
        self.binding, self.oid, self.valid = binding, oid, valid
        self.commands = []
    @asynccontextmanager
    async def transaction(self):
        yield
    async def execute(self, statement, params=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        self.commands.append(text)
        if "SELECT role_name::text,role_oid" in text:
            return Cursor(self.binding)
        if "SELECT oid FROM pg_catalog.pg_roles" in text:
            return Cursor(None if self.oid is None else (self.oid,))
        if "_runtime_role_is_valid" in text:
            return Cursor((self.valid,))
        if text.startswith("CREATE ROLE"):
            self.oid = 101
        if "SELECT current_database()" in text:
            return Cursor(("synthetic_db",))
        return Cursor(None)


def invoke(monkeypatch, connection, *, base=BASE, rotate=False):
    @asynccontextmanager
    async def direct(*args, **kwargs):
        yield connection
    async def migrate(*args, **kwargs):
        return 3, 3
    class Database:
        async def open(self):
            pass
        async def close(self):
            pass
        def __init__(self, *args, **kwargs):
            pass
    monkeypatch.setattr(roles, "direct_connection", direct)
    monkeypatch.setattr(roles, "migrate", migrate)
    monkeypatch.setattr(roles, "RuntimeDatabase", Database)
    monkeypatch.setattr(roles.secrets, "token_urlsafe", lambda count: "synthetic-new-secret")
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:
        return runner.run(roles.provision_role(DIRECT, base, rotate=rotate))


@pytest.mark.parametrize("rotate", [False, True])
def test_unsafe_managed_role_is_rejected_before_any_mutation(monkeypatch, rotate):
    connection = Connection((roles.RUNTIME_ROLE, 101), 101, valid=False)
    with pytest.raises(roles.RoleProvisioningError):
        invoke(monkeypatch, connection, rotate=rotate)
    assert not any(text.startswith(("CREATE", "ALTER", "GRANT", "INSERT", "REVOKE")) for text in connection.commands)


@pytest.mark.parametrize("binding,actual", [(None, 999), ((roles.RUNTIME_ROLE, 101), 999), (("foreign", 101), 101)])
def test_foreign_or_recreated_role_is_not_adopted_or_rotated(monkeypatch, binding, actual):
    connection = Connection(binding, actual)
    with pytest.raises(roles.RoleProvisioningError):
        invoke(monkeypatch, connection, rotate=True)
    assert not any(text.startswith(("CREATE", "ALTER", "GRANT", "INSERT", "REVOKE")) for text in connection.commands)


def test_valid_managed_rerun_preserves_password_and_has_no_mutation(monkeypatch):
    connection = Connection((roles.RUNTIME_ROLE, 101), 101)
    assert invoke(monkeypatch, connection) is BASE
    assert not any(text.startswith(("CREATE", "ALTER", "GRANT", "INSERT", "REVOKE")) for text in connection.commands)


def test_lost_local_runtime_credential_requires_explicit_recovery(monkeypatch):
    connection = Connection((roles.RUNTIME_ROLE, 101), 101)
    with pytest.raises(roles.RoleProvisioningError):
        invoke(monkeypatch, connection, base=replace(BASE, user="admin"))
    assert not any("PASSWORD" in text for text in connection.commands)


def test_explicit_recovery_changes_password_only_after_privilege_preflight(monkeypatch):
    connection = Connection((roles.RUNTIME_ROLE, 101), 101)
    result = invoke(monkeypatch, connection, rotate=True)
    assert result.password == "synthetic-new-secret"
    preflight = next(i for i, text in enumerate(connection.commands) if "_runtime_role_is_valid" in text)
    password = next(i for i, text in enumerate(connection.commands) if "PASSWORD" in text)
    assert preflight < password
    assert not any(text.startswith(("CREATE", "GRANT", "INSERT")) for text in connection.commands)


def test_new_role_with_unsafe_access_is_rejected_before_password(monkeypatch):
    connection = Connection(None, None, valid=False)
    with pytest.raises(roles.RoleProvisioningError):
        invoke(monkeypatch, connection, base=replace(BASE, user="admin"))
    assert any(text.startswith("CREATE ROLE") for text in connection.commands)
    assert not any("PASSWORD" in text for text in connection.commands)
