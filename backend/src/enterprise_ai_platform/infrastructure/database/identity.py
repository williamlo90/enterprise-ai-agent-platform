"""Restricted, parameterized identity capability; never uses migration credentials."""

from psycopg import sql
from psycopg.types.json import Jsonb

from enterprise_ai_platform.modules.identity.ports import IdentityFailure, BrowserContextFailure, browser_read_scope
from enterprise_ai_platform.infrastructure.database.browser_context import context_guard


class PostgresIdentityRepository:
    def __init__(self, database):
        self.database = database

    async def call(self, operation, payload):
        try:
            async with self.database.transaction() as connection:
                cursor = await connection.execute(sql.SQL("SELECT {}.identity_call(%s,%s)").format(
                    sql.Identifier(self.database.schema)), (operation, Jsonb(payload)))
                result = (await cursor.fetchone())[0]
                context = browser_read_scope.get()
                bound = None
                if context is not None and operation in ('me','switch') and result.get('status') == 'ok':
                    context.check_owner()
                    if context.mode != 'bootstrap':
                        raise BrowserContextFailure()
                    token = payload['new_token_hash'] if operation == 'switch' else payload['token_hash']
                    bound = await context_guard(self.database,connection,token,None)
                    fields = ('principal_id','active_tenant_id','active_membership_id','access_revision')
                    if any(result['identity'].get(key) != bound[key] for key in fields):
                        raise BrowserContextFailure(409)
                    result = {**result,'identity':{**result['identity'],'context_binding':bound['context_binding']}}
            if bound is not None:
                context.check_owner()
                context.attestation = bound
            return result
        except BrowserContextFailure:
            raise
        except Exception:
            raise IdentityFailure(503) from None
