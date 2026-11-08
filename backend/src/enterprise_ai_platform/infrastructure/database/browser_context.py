"""Binding checks share the feature connection and only attest after its commit."""
import re
import math
from psycopg import sql
from enterprise_ai_platform.modules.identity.ports import browser_read_scope, BrowserContextFailure


async def context_guard(database, connection, token, expected):
    row = (await (await connection.execute(sql.SQL('SELECT {}.browser_context_guard(%s,%s)').format(
        sql.Identifier(database.schema)), (token,expected))).fetchone())[0]
    if not isinstance(row,dict) or row.get('status') != 'ok':
        raise BrowserContextFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'conflict':409}.get(
            row.get('status') if isinstance(row,dict) else None,503))
    if not isinstance(row.get('context_binding'),str) or not re.fullmatch('[a-f0-9]{64}',row['context_binding']) or type(row.get('authority_expires_at')) not in (int,float) or not math.isfinite(row['authority_expires_at']):
        raise BrowserContextFailure()
    return row


async def before_read(database, connection, token, family):
    context = browser_read_scope.get()
    if context is None:
        return None
    context.before(token,family)
    return await context_guard(database,connection,token,context.expected)


async def after_read(database, connection, token, family):
    context = browser_read_scope.get()
    if context is None:
        return None
    context.check_owner()
    if context.token_hash != token or context.family != family:
        raise BrowserContextFailure()
    return await context_guard(database,connection,token,context.expected)


def committed_read(token, family, operation, row):
    context = browser_read_scope.get()
    if context is not None:
        context.committed(token,family,operation,row)
