"""Owner UoW remains held while typed provenance is checked; nothing streams raw."""
import asyncio
import sys
import json
from enterprise_ai_platform.infrastructure.database.service_sources import staged,ServiceStage
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.conversations.domain import ConversationFailure,SourceSubject,message_refs,public_messages
from enterprise_ai_platform.infrastructure.database.document_sources import DatabaseDocumentSources
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.infrastructure.database.browser_context import before_read, after_read, committed_read

class PostgresConversationRepository:
    def __init__(self,database,*,live_sources=False,service_sources=None):self.database,self.live_sources,self.service_sources=database,live_sources,service_sources
    async def run(self,operation,payload,sources):
        from enterprise_ai_platform.infrastructure.database.checkpoints import PostgresCheckpointRepository
        if operation in ('read','diagnostics','recover'):
            try:
                async with self.database.transaction() as connection:
                    await before_read(self.database,connection,payload['token'],'conversations')
                    if operation=='read':
                        result=await PostgresCheckpointRepository(self.database,sources).execute(connection,operation,payload)
                    else:
                        result=(await (await connection.execute(sql.SQL('SELECT {}.rag_owner_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(payload['token'],operation,Jsonb({k:v for k,v in payload.items() if k!='token'})))).fetchone())[0]
                    if not isinstance(result,dict) or result.get('status')!='ok':raise ConversationFailure({'missing':404,'conflict':409,'invalid':400,'forbidden':403,'unauthenticated':401}.get(result.get('status') if isinstance(result,dict) else None,503))
                    bound=await after_read(self.database,connection,payload['token'],'conversations')
                committed_read(payload['token'],'conversations',operation,bound)
                return {k:v for k,v in result.items() if k!='status'}
            except BrowserContextFailure:raise
            except ConversationFailure:raise
            except Exception:raise ConversationFailure(503) from None
        guard=PostgresCheckpointRepository(self.database,sources,service_sources=self.service_sources)
        if self.live_sources:guard.source_factory=lambda connection:DatabaseDocumentSources(self.database,connection,dict(token=payload['token']),history=False)
        result=await guard.call(operation,payload)
        return {k:v for k,v in result.items() if k!='status'}
    async def execute(self,connection,token,operation,payload):
        if operation in {'assisted_append','assisted_read'}:
            result=(await (await connection.execute(sql.SQL('SELECT {}.assisted_owner_call(%s,%s)').format(sql.Identifier(self.database.schema)),(token,Jsonb(payload)))).fetchone())[0]
        else:
            result=(await (await connection.execute(sql.SQL('SELECT {}.conversation_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))).fetchone())[0]
        if not isinstance(result,dict) or result.get('status')!='ok':
            state=result.get('status') if isinstance(result,dict) else None
            raise ConversationFailure({'unauthenticated':401,'forbidden':403,'missing':404,'invalid':400,'conflict':409,'capacity':429}.get(state,503))
        return {key:value for key,value in result.items() if key!='status'}
    async def call(self,token,operation,payload):
        try:
            async with self.database.transaction() as connection:
                await before_read(self.database,connection,token,'conversations')
                result=await self.execute(connection,token,operation,payload)
                bound=await after_read(self.database,connection,token,'conversations')
            committed_read(token,'conversations',operation,bound)
            return result
        except BrowserContextFailure:raise
        except ConversationFailure:raise
        except Exception:raise ConversationFailure(503) from None
    async def history(self,token,payload,sources):
        if self.live_sources and self.service_sources is not None:
            return await staged(lambda gate:self._history(token,payload,sources,gate),self.service_sources,redact=True)
        return await self._history(token,payload,sources)
    async def _history(self,token,payload,sources,service_gate=None):
        transaction=self.database.transaction();entered=False;closed=False
        try:
            connection=await transaction.__aenter__();entered=True
            await before_read(self.database,connection,token,'conversations')
            first=await self.execute(connection,token,'history',payload)
            subject=SourceSubject(first['tenant_id'],first['principal_id'])
            refs=tuple(dict.fromkeys(ref for message in first['messages'] for ref in message_refs(message)))
            bindings=[dict(id=message['id'],sequence=message['sequence'],snapshot=message['_snapshot']) for message in first['messages']]
            async with asyncio.timeout(5):
                source_access=DatabaseDocumentSources(self.database,connection,dict(token=token),history=True,service_gate=service_gate) if self.live_sources else sources
                async with source_access.hold(subject,refs) as decisions:
                    if not isinstance(decisions,tuple) or len(decisions)!=len(refs) or any(type(value) is not bool for value in decisions):raise ConversationFailure(503)
                    if self.live_sources and self.service_sources is not None and any(r.system=='service' for r in refs):
                        signature=json.dumps(dict(version=first['conversation']['version'],bindings=bindings),sort_keys=True)
                        if service_gate is None:raise ServiceStage(source_access.service_context,refs,signature)
                        decisions=service_gate.merge(source_access,refs,decisions,signature)
                    final=await self.execute(connection,token,'history_recheck',dict(id=payload['id'],expected_version=first['conversation']['version'],bindings=bindings))
                    final['messages']=public_messages(final['messages'],dict(zip(refs,decisions)))
                    final['next_after']=first['next_after']
                    bound=await after_read(self.database,connection,token,'conversations')
                    # Commit owner validation while source guard is still held.
                    closed=True;await transaction.__aexit__(None,None,None)
                    committed_read(token,'conversations','history_recheck',bound)
                return final
        except BrowserContextFailure:raise
        except ConversationFailure:raise
        except ServiceStage:raise
        except Exception:raise ConversationFailure(503) from None
        finally:
            if entered and not closed:
                try:await transaction.__aexit__(*sys.exc_info())
                except Exception:pass
