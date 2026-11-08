"""Narrow policy RPC: approval, snapshot and receipt share the original UoW."""
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.modules.privacy.ports import PrivacyDownloadExpired
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read


def checked(value):
    if type(value) is dict and value.get('status')=='expired':raise PrivacyDownloadExpired()
    if type(value) is not dict or value.get('status')!='ok':
        raise AccessFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'conflict':409,'expired':410,'held':503}.get(value.get('status') if type(value) is dict else None,503))
    return value


class PostgresPrivacyPolicyRepository:
    def __init__(self,database):self.database=database
    async def call(self,token,operation,payload):
        try:
            effect=operation in {'effect_publish','effect_current','effect_effective'}
            fence=operation in {'fence','fence_status','release','export','delete'}
            rpc_operation=operation.removeprefix('effect_') if effect else operation
            family='privacy_effect' if effect else 'privacy_conversations' if fence or operation=='inventory' else 'privacy'
            async with self.database.transaction() as connection:
                query=sql.SQL('SELECT {}.privacy_conversation_delete_call(%s,%s,%s)' if operation=='delete' else 'SELECT {}.privacy_conversation_export_call(%s,%s,%s)' if operation=='export' else 'SELECT {}.privacy_conversation_release_call(%s,%s,%s)' if operation=='release' else 'SELECT {}.privacy_conversation_fence_call(%s,%s,%s)' if fence else 'SELECT {}.privacy_effect_policy_call(%s,%s,%s)' if effect else 'SELECT {}.privacy_conversation_inventory(%s,%s)' if operation=='inventory' else 'SELECT {}.privacy_policy_call(%s,%s,%s)').format(sql.Identifier(self.database.schema))
                if rpc_operation in {'publish','fence','release','export','delete'}:checked((await (await connection.execute(query,(token,'lock',Jsonb(payload)))).fetchone())[0])
                await before_read(self.database,connection,token,family)
                args=(token,Jsonb(payload)) if operation=='inventory' else (token,rpc_operation,Jsonb(payload))
                result=checked((await (await connection.execute(query,args)).fetchone())[0])
                bound=await after_read(self.database,connection,token,family)
            committed_read(token,family,rpc_operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (AccessFailure,BrowserContextFailure,PrivacyDownloadExpired):raise
        except Exception:raise AccessFailure(503) from None
