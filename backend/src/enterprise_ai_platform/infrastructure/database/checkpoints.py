"""Every operation pins source decisions through the fenced owner transaction."""
import asyncio
import sys
import time
import math
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.service_sources import staged,ServiceStage
from enterprise_ai_platform.infrastructure.database.document_sources import DatabaseDocumentSources
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.conversations.ports import SourceSubject, SourceRef, ConversationFailure
from enterprise_ai_platform.infrastructure.database.connection import rpc_result


class PostgresCheckpointRepository:
    def __init__(self, database, sources, *, source_factory=None, lock=None, service_sources=None):
        self.database, self.sources = database, sources
        self.source_factory=source_factory
        self.service_sources=service_sources
        self._lock = lock if lock is not None else asyncio.Lock()

    async def execute(self, connection, operation, payload):
        if 'token' in payload:
            result = await rpc_result(connection,sql.SQL('SELECT {}.checkpoint_owner_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)), (payload['token'],operation,Jsonb({k:v for k,v in payload.items() if k!='token'})))
        else:
            result = await rpc_result(connection,sql.SQL('SELECT {}.checkpoint_call(%s,%s)').format(sql.Identifier(self.database.schema)), (operation, Jsonb(payload)))
        if not isinstance(result, dict) or result.get('status') != 'ok':
            error=ConversationFailure({'invalid':400,'missing':404,'conflict':409,'stale':409,'forbidden':403,'held':403,'unauthenticated':401,'capacity':429}.get(result.get('status') if isinstance(result,dict) else None,503))
            error.rejection_reason=result.get('reason') if isinstance(result,dict) and result.get('reason') in {'parent','checkpoint','channel_or_path','value'} else 'unspecified'
            raise error
        return result

    async def call(self, operation, payload):
        # LangGraph schedules checkpoint and pending-write persistence concurrently.
        # Serialize one bound repository, never share an open UoW with graph nodes.
        async with self._lock:
            if self.service_sources is not None:
                return await staged(lambda gate:self._call(operation,payload,gate),self.service_sources)
            return await self._call(operation,payload)

    async def _call(self, operation, payload, service_gate=None):
        transaction = self.database.transaction(); entered = closed = False
        try:
            connection = await transaction.__aenter__(); entered = True
            if 'token' in payload:
                from enterprise_ai_platform.infrastructure.database.browser_context import before_read,after_read,committed_read
                await before_read(self.database,connection,payload['token'],'conversations')
            first = await self.execute(connection, 'stage', payload)
            refs = tuple(SourceRef(**value) for value in first['sources'])
            async with asyncio.timeout(5):
                source_access=self.source_factory(connection) if self.source_factory else self.sources
                if self.service_sources is not None:
                    if 'token' in payload:source_access=DatabaseDocumentSources(self.database,connection,dict(token=payload['token']),history=False,service_gate=service_gate)
                    elif isinstance(source_access,DatabaseDocumentSources) and service_gate is not None:source_access.auth['service_proofs']=service_gate.envelopes;source_access.service_gate=service_gate
                async with source_access.hold(SourceSubject(first['tenant_id'], first['principal_id']), refs) as decisions:
                    if not isinstance(decisions,tuple) or len(decisions)!=len(refs) or any(type(v) is not bool for v in decisions):raise ConversationFailure(503)
                    if self.service_sources is not None and any(r.system=='service' for r in refs):
                        if operation not in ('get','list'):raise ConversationFailure(403)
                        signature=(first['tenant_id'],first['principal_id'],payload.get('id'),payload.get('run_id'),payload.get('generation'),payload.get('epoch'),payload.get('expected_version'))
                        if service_gate is None:raise ServiceStage(source_access.service_context,refs,signature)
                        decisions=service_gate.merge(source_access,refs,decisions,signature)
                    if not all(decisions):raise ConversationFailure(403)
                    final = await self.execute(connection, operation, payload)
                    if 'token' in payload:bound=await after_read(self.database,connection,payload['token'],'conversations')
                    closed = True; await transaction.__aexit__(None,None,None)
                    if 'token' in payload:committed_read(payload['token'],'conversations',operation,bound)
                deadline=final.get('authority_expires_at')
                if type(deadline) not in (int,float) or not math.isfinite(deadline) or time.time()>=deadline:raise ConversationFailure(401)
                return final
        except BrowserContextFailure:raise
        except ConversationFailure:raise
        except ServiceStage:raise
        except Exception:raise ConversationFailure(503) from None
        finally:
            if entered and not closed:
                try:await transaction.__aexit__(*sys.exc_info())
                except Exception:pass
