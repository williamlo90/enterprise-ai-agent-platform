import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys

import pytest
from psycopg import sql

from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import MigrationSettings, load_settings
from enterprise_ai_platform.infrastructure.database import migration
from enterprise_ai_platform.infrastructure.database.connection import parse_target
from enterprise_ai_platform.infrastructure.database.migration import MigrationError, Revision, revisions, validate_history, validate_revisions, revision_sql


def test_packaged_revisions_have_contiguous_history_and_renderable_identifiers():
    items = revisions()
    assert [item.version for item in items] == list(range(1,31))
    assert [item.name for item in items] == ["0001_foundation.sql", "0002_foundation_indexes.sql", "0003_runtime_isolation.sql", "0004_identity.sql", "0005_authorization.sql", "0006_delegation.sql", "0007_jobs.sql", "0008_files.sql", "0009_knowledge.sql", "0010_knowledge_lifecycle.sql", "0011_conversations.sql", "0012_checkpoints.sql", "0013_rag.sql", "0014_rag_runtime.sql", "0015_browser_context.sql", "0016_conversation_labels.sql", "0017_knowledge_catalog.sql", "0018_knowledge_effective_period.sql", "0019_organization.sql", "0020_knowledge_audience.sql", "0021_policies.sql", "0022_receipt_pins.sql", "0023_receipt_download.sql", "0024_drafts.sql", "0025_actions.sql", "0026_workflow.sql", "0027_assisted_workflow.sql", "0028_workflow_workspace.sql", "0029_administration.sql", "0030_service_provenance.sql"]
    for item in items:
        assert len(item.checksum) == 64
        rendered = revision_sql(item, "platform").as_string()
        assert '"platform".' in rendered
        assert "{schema}" not in rendered
    assert "DEFERRABLE INITIALLY DEFERRED" in items[0].source
    assert "ON DELETE CASCADE" not in items[0].source


@pytest.mark.parametrize("items", [(), (Revision(2, "b", "select 1"),),
    (Revision(1, "a", "select 1"), Revision(3, "b", "select 1")),
    (Revision(1, "a", ""),), (Revision(1, "same", "select 1"), Revision(2, "same", "select 2"))])
def test_invalid_revision_manifests_are_rejected(items):
    with pytest.raises(MigrationError):
        validate_revisions(items)


@pytest.mark.parametrize("case", ["checksum", "gap", "unknown", "name", "order"])
def test_ledger_must_be_an_exact_prefix(case):
    items = revisions()
    history = [(item.version, item.name, item.checksum) for item in items]
    if case == "checksum":
        history[0] = (1, items[0].name, "0" * 64)
    elif case == "gap":
        history = history[1:]
    elif case == "unknown":
        history.append((len(items) + 1, "foreign", "0" * 64))
    elif case == "name":
        history[0] = (1, "foreign", items[0].checksum)
    else:
        history.reverse()
    with pytest.raises(MigrationError):
        validate_history(history, items)


def test_empty_partial_and_full_history_are_valid():
    items = revisions()
    for size in range(len(items) + 1):
        validate_history([(item.version, item.name, item.checksum) for item in items[:size]], items)


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows
    async def fetchone(self):
        return self.row
    async def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, history=(), unmanaged=False, ledger_exists=False):
        self.history = list(history)
        self.unmanaged = unmanaged
        self.ledger_exists = ledger_exists
        self.commands = []
    async def execute(self, statement, params=None, **kwargs):
        text = statement if isinstance(statement, str) else statement.as_string()
        self.commands.append((text, params))
        if "SELECT EXISTS" in text:
            return Cursor(row=(bool(self.history) or self.ledger_exists if "schema_migrations'" in text else self.unmanaged,))
        if "SELECT version, name, checksum" in text:
            return Cursor(rows=self.history)
        return Cursor()
    @asynccontextmanager
    async def transaction(self):
        self.commands.append(("BEGIN", None))
        try:
            yield
        except BaseException:
            self.commands.append(("ROLLBACK", None))
            raise
        else:
            self.commands.append(("COMMIT", None))


def invoke(monkeypatch, connection, **kwargs):
    @asynccontextmanager
    async def direct(*args, **kwargs):
        yield connection
    monkeypatch.setattr(migration, "direct_connection", direct)
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:
        return runner.run(migration.migrate(parse_target("postgres://user:synthetic@host/db"), **kwargs))


def test_status_is_read_only_and_never_creates_schema(monkeypatch):
    connection = Connection()
    assert invoke(monkeypatch, connection) == (0, len(revisions()))
    text = "\n".join(item[0] for item in connection.commands)
    assert "READ ONLY" in text
    assert "CREATE" not in text
    assert "INSERT" not in text
    assert "pg_advisory" not in text


def test_lock_precedes_history_and_all_pending_sql_and_ledger_share_transaction(monkeypatch):
    connection = Connection()
    assert invoke(monkeypatch, connection, upgrade=True) == (len(revisions()), len(revisions()))
    statements = [item[0] for item in connection.commands]
    lock = next(i for i, value in enumerate(statements) if "pg_advisory_xact_lock" in value)
    history = next(i for i, value in enumerate(statements) if "SELECT EXISTS" in value)
    create = next(i for i, value in enumerate(statements) if "CREATE SCHEMA" in value)
    assert 0 < lock < history < create < len(statements) - 1
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"
    assert not any(prefix + " IF NOT EXISTS" in text for text in statements for prefix in ("CREATE SCHEMA", "CREATE TABLE", "CREATE INDEX", "CREATE ROLE"))
    inserts = [params for text, params in connection.commands if text.startswith('INSERT INTO "platform".schema_migrations')]
    assert inserts == [(item.version, item.name, item.checksum) for item in revisions()]


@pytest.mark.parametrize("kind", ["unmanaged", "empty-ledger", "drift", "downgrade"])
def test_invalid_state_rolls_back_without_schema_writes(monkeypatch, kind):
    items = revisions()
    history = [(item.version, item.name, item.checksum) for item in items]
    connection = Connection(history if kind not in {"unmanaged", "empty-ledger"} else (),
                            unmanaged=kind == "unmanaged", ledger_exists=kind == "empty-ledger")
    kwargs = {"upgrade": True}
    if kind == "drift":
        connection.history[0] = (1, items[0].name, "0" * 64)
    if kind == "downgrade":
        kwargs["target_version"] = 1
    with pytest.raises(MigrationError):
        invoke(monkeypatch, connection, **kwargs)
    assert connection.commands[-1][0] == "ROLLBACK"
    assert not any(text.startswith(("CREATE", "INSERT", "ALTER")) for text, _ in connection.commands)


@pytest.mark.parametrize("schema", ["public", "pg_catalog", "platform; DROP SCHEMA public", "db02_probe_other"])
def test_schema_override_rejects_unowned_and_system_schemas(monkeypatch, schema):
    connection = Connection()
    with pytest.raises(MigrationError):
        invoke(monkeypatch, connection, schema=schema, upgrade=True)
    assert not connection.commands


def test_migration_settings_do_not_require_runtime_url(tmp_path):
    file = tmp_path / ".env"
    file.write_text("APP_ENV=test\nAPP_HOST=127.0.0.1\nAPP_PORT=8000\nDATABASE_DIRECT_URL=postgres://user:synthetic@host/db\n", encoding="utf-8")
    settings = load_settings(file, settings_type=MigrationSettings)
    assert settings.database_url is None
    assert "synthetic" not in repr(settings)


def test_cli_missing_configuration_fails_safely():
    result = subprocess.run([sys.executable, "-m", "enterprise_ai_platform.migration_cli", "status", "--no-env-file"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert result.stderr == "Migration configuration missing or invalid.\n"


def test_static_resource_import_is_allowed_only_in_adapter(tmp_path):
    root = Path(__file__).resolve().parents[3]
    for relative, source, expected in [
        ("infrastructure/database/assets.py", "from importlib.resources import files", 0),
        ("infrastructure/database/assets.py", "from importlib import import_module", 1),
        ("modules/system/ports.py", "from importlib.resources import files", 1),
    ]:
        file = tmp_path / "backend/src/enterprise_ai_platform" / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source, encoding="utf-8")
        result = subprocess.run([sys.executable, str(root / "scripts/checks/check_architecture.py"), "--root", str(tmp_path)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == expected
        file.unlink()
