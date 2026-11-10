"""Restricted worker RPC; document source guards reuse the enclosing owner UoW."""
from contextlib import asynccontextmanager
from dataclasses import asdict
import asyncio
import math
import time
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.rag.ports import RagFailure
from enterprise_ai_platform.infrastructure.database.document_sources import DatabaseDocumentSources
from enterprise_ai_platform.infrastructure.database.connection import rpc_result

class PostgresRagRepository:
    def __init__(self,database,binding,*,lock=None):
        self.database,self.binding=database,dict(binding)
        self._lock=lock if lock is not None else asyncio.Lock()
    async def execute(self,connection,operation,payload):
        row=await rpc_result(connection,sql.SQL('SELECT {}.rag_call(%s,%s)').format(sql.Identifier(self.database.schema)),(operation,Jsonb({**self.binding,**payload})))
        if not isinstance(row,dict) or row.get('status')!='ok':
            raise RagFailure({'invalid':400,'forbidden':403,'held':403,'unauthenticated':401,'missing':404,'conflict':409,'stale':409,'capacity':429}.get(row.get('status') if isinstance(row,dict) else None,503))
        return row
    async def call(self,operation,payload):
        async with self._lock:return await self._call(operation,payload)
    @asynccontextmanager
    async def lease_slot(self):
        async with self._lock:yield
    async def _call(self,operation,payload):
        try:
            async with self.database.transaction() as connection:result=await self.execute(connection,operation,payload)
            deadline=result.get('authority_expires_at')
            if type(deadline) not in (int,float) or not math.isfinite(deadline) or time.time()>=deadline:raise RagFailure(401)
            return result
        except RagFailure:raise
        except Exception:raise RagFailure(503) from None
    async def runtime(self,operation,payload):
        async with self._lock:
            try:
                async with self.database.transaction() as connection:
                    row=await rpc_result(connection,sql.SQL('SELECT {}.rag_runtime_call(%s,%s)').format(sql.Identifier(self.database.schema)),(operation,Jsonb({**self.binding,**payload})))
                    if not isinstance(row,dict) or row.get('status')!='ok':raise RagFailure({'invalid':400,'held':429,'forbidden':403,'stale':409,'conflict':409}.get(row.get('status') if isinstance(row,dict) else None,503))
                return row
            except RagFailure:raise
            except Exception:raise RagFailure(503) from None
    async def settle(self,kind,receipt,usage):
        # No content/state authority: narrow receipt may record late usage after
        # the writer lease/session has expired, without reviving that writer.
        values=dict(tenant_id=self.binding['tenant_id'],run_id=self.binding['run_id'],kind=kind,receipt=receipt,input_tokens=usage.input_tokens if usage is not None else None,output_tokens=usage.output_tokens if usage is not None else None)
        try:
            async with self.database.transaction() as connection:
                row=(await (await connection.execute(sql.SQL('SELECT {}.rag_usage_call(%s)').format(sql.Identifier(self.database.schema)),(Jsonb(values),))).fetchone())[0]
                if not isinstance(row,dict) or row.get('status')!='ok':raise RagFailure(503)
            return row
        except RagFailure:raise
        except Exception:raise RagFailure(503) from None
    def source_access(self,connection):
        return DatabaseDocumentSources(self.database,connection,self.binding,history=False)
