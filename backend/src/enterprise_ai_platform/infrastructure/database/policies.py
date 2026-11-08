"""Policy capabilities own their transactions; HTTP dispatch holds no DB locks."""
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.policies.ports import PolicyFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read


class PostgresPolicyRepository:
    def __init__(self,database):self.database=database

    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'policies')
                result=(await (await connection.execute(sql.SQL('SELECT {}.policy_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
                bound=await after_read(self.database,connection,token,'policies') if result.get('status')=='ok' else None
            if result.get('status')!='ok':raise PolicyFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'conflict':409}.get(result.get('status'),503))
            committed_read(token,'policies',operation,bound)
            return {k:v for k,v in result.items() if k!='status'}
        except (PolicyFailure,BrowserContextFailure):raise
        except Exception:raise PolicyFailure(503) from None

    async def eligibility(self,actor,request,credential_hash,verification_ref):
        try:
            async with self.database.transaction() as connection:
                auth={'actor':actor,'credential_hash':credential_hash,'verification_ref':verification_ref}
                result=(await (await connection.execute(sql.SQL('SELECT {}.policy_eligibility_call(%s,%s)').format(sql.Identifier(self.database.schema)),(Jsonb(auth),Jsonb(request)))).fetchone())[0]
            if result.get('status')!='ok':raise PolicyFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404,'conflict':409}.get(result.get('status'),503))
            return {k:v for k,v in result.items() if k!='status'}
        except PolicyFailure:raise
        except Exception:raise PolicyFailure(503) from None
