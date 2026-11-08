"""Packaged migration examples. Feature owners adapt them in new immutable revisions."""

from importlib.resources import files
import re
from psycopg import sql
from enterprise_ai_platform.infrastructure.database.connection import direct_connection,bounded_transaction
from enterprise_ai_platform.infrastructure.database.migration import MigrationError,Revision,revision_sql,access_role

STAGES=frozenset({"create","expand","backfill","contract"})


def pattern_source(stage,feature,*,batch_size=100,legacy_clients_retired=False):
    if type(stage) is not str or stage not in STAGES or type(feature) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}",feature):
        raise MigrationError()
    if type(batch_size) is not int or not 1<=batch_size<=1000:
        raise MigrationError()
    if stage=="contract" and legacy_clients_retired is not True:
        raise MigrationError()
    source=files("enterprise_ai_platform.infrastructure.database").joinpath("patterns","feature_"+stage+".sql").read_text(encoding="utf-8").replace("\r\n","\n")
    names={"parent":"_collections","entry":"_entries","pointer_fk":"_current_entry_fk","owner_index":"_owner_idx",
        "parent_index":"_parent_idx","display_constraint":"_display_check","sync_function":"_label_sync",
        "sync_trigger":"_label_sync_trigger","backfill_index":"_backfill_idx"}
    return sql.SQL(source).format(schema=sql.SQL("{schema}"),access_role=sql.SQL("{access_role}"),
        batch_size=sql.Literal(batch_size),**{name:sql.Identifier(feature+suffix) for name,suffix in names.items()}).as_string()


async def backfill_batch(target,*,schema,feature,batch_size=100):
    access_role(schema)  # Only the application or owned probe namespaces.
    statement=revision_sql(Revision(1,"0001_batch.sql",pattern_source("backfill",feature,batch_size=batch_size)),schema)
    async with direct_connection(target,timeout=30) as connection:
        async with bounded_transaction(connection):
            await connection.execute("SET LOCAL search_path=pg_catalog")
            await connection.execute("SET LOCAL statement_timeout='20s'")
            await connection.execute("SET LOCAL lock_timeout='5s'")
            cursor=await connection.execute(statement)
            return (await cursor.fetchone())[0]
