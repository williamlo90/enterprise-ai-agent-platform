from pathlib import Path
import asyncio
import pytest
from enterprise_ai_platform.infrastructure.database.migration import MigrationError,Revision,revision_sql,validate_revisions
from enterprise_ai_platform.infrastructure.database.migration_sql import validate_atomic_sql,statement_words
from enterprise_ai_platform.infrastructure.database.migration_patterns import pattern_source


@pytest.mark.parametrize("command",["COMMIT","END WORK","ABORT","ROLLBACK","BEGIN","START TRANSACTION","SAVEPOINT x","RELEASE x",
    "PREPARE TRANSACTION 'x'","VACUUM x","COPY x TO STDOUT","ALTER SYSTEM SET x='y'","CREATE DATABASE x","DROP TABLESPACE x",
    "CREATE INDEX CONCURRENTLY x ON y(z)","DROP INDEX CONCURRENTLY x","REINDEX INDEX CONCURRENTLY x","SET search_path=public",
    "SET LOCAL standard_conforming_strings=off","RESET ALL","DISCARD ALL"])
def test_nontransactional_or_session_control_sql_is_rejected_before_any_work(command):
    with pytest.raises(MigrationError):validate_revisions((Revision(1,"0001_test.sql","SELECT 1; "+command+"; SELECT 1/0;"),))


@pytest.mark.parametrize("source",[
    "SELECT 'COMMIT; ROLLBACK';",
    'SELECT "commit" FROM "rollback";',
    "DO $body$ BEGIN RAISE NOTICE 'COMMIT'; END; $body$; SELECT 1;",
    "/* outer /* COMMIT; */ ROLLBACK; */ SELECT 1; -- COMMIT;\nSELECT 2;",
    "SELECT E'escaped\\\' COMMIT; still inside'; SELECT 2;",
    "SET LOCAL lock_timeout='1s'; SET CONSTRAINTS ALL DEFERRED; SELECT 1;",
    "SELECT foo$tag$ FROM example;",
])
def test_comments_strings_functions_and_local_controls_are_not_false_positives(source):
    validate_atomic_sql(source)


@pytest.mark.parametrize("source",["-- comment\rCOMMIT;","-- comment\nCOMMIT;","SELECT 1; /* nested /* x */ y */ COMMIT;",
    "SELECT 1; 'unterminated", "DO $body$ BEGIN SELECT 1;", "/* unterminated", "-- only comment"])
def test_ambiguous_or_unterminated_sql_fails_closed(source):
    with pytest.raises(ValueError):validate_atomic_sql(source)


@pytest.mark.parametrize("stage",["create","expand","backfill","contract"])
def test_packaged_patterns_render_and_are_transactional(stage):
    source=pattern_source(stage,"fixture",legacy_clients_retired=True)
    validate_atomic_sql(source)
    rendered=revision_sql(Revision(1,"0001_fixture.sql",source),"platform").as_string()
    assert '{schema}' not in rendered and '{access_role}' not in rendered
    assert '"platform"."fixture_' in rendered


@pytest.mark.parametrize("feature",["bad;DROP SCHEMA public","_internal","x"*25,"Upper",None])
def test_pattern_identifiers_are_bounded_and_not_sql(feature):
    with pytest.raises(MigrationError):pattern_source("create",feature)


@pytest.mark.parametrize("value",[0,-1,1001,True,"2"])
def test_backfill_batch_size_is_bounded(value):
    with pytest.raises(MigrationError):pattern_source("backfill","fixture",batch_size=value)


def test_contract_needs_explicit_legacy_client_retirement_gate():
    with pytest.raises(MigrationError):pattern_source("contract","fixture")
    with pytest.raises(MigrationError):pattern_source("../../foreign","fixture")


def test_cli_explicit_target_is_forwarded_instead_of_implicitly_upgrading_head(monkeypatch,capsys):
    from types import SimpleNamespace
    import sys
    from enterprise_ai_platform import migration_cli
    calls=[]
    async def staged(target,**kwargs):
        calls.append(kwargs)
        return 3,3
    monkeypatch.setattr(sys,"argv",["migration","upgrade","--no-env-file","--target-version","3"])
    monkeypatch.setattr(migration_cli,"load_settings",lambda *a,**k:SimpleNamespace(database_direct_url=SimpleNamespace(get_secret_value=lambda:"postgresql://fixture:fictional@fixture.example.test/fixture")))
    monkeypatch.setattr(migration_cli,"migrate",staged)
    migration_cli.main()
    assert calls==[{"upgrade":True,"target_version":3}]
    assert "revision 0003, target 0003" in capsys.readouterr().out
