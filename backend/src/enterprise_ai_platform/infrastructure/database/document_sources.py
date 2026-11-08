"""Live source guard on the caller's existing owner/worker connection."""
from contextlib import asynccontextmanager
from dataclasses import asdict
import math
import time
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure

class DatabaseDocumentSources:
    def __init__(self,database,connection,auth,*,history,service_gate=None):
        self.database,self.connection,self.auth,self.history=database,connection,dict(auth),history
        if service_gate is not None:self.auth['service_proofs']=service_gate.envelopes
        self.service_context={};self.service_eligible=();self.service_gate=service_gate
    @asynccontextmanager
    async def hold(self,subject,refs):
        result=(await (await self.connection.execute(sql.SQL('SELECT {}.document_sources_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(Jsonb(self.auth),Jsonb([asdict(ref) for ref in refs]),self.history))).fetchone())[0]
        if not isinstance(result,dict) or result.get('status')!='ok':raise ConversationFailure({'unauthenticated':401,'forbidden':403,'missing':404}.get(result.get('status') if isinstance(result,dict) else None,503))
        if (result.get('tenant_id'),result.get('principal_id'))!=(subject.tenant_id,subject.principal_id):raise ConversationFailure(403)
        values=result.get('decisions');deadline=result.get('authority_expires_at')
        self.service_context=result.get('service_context',{})
        eligible=result.get('service_eligible',[False]*len(refs))
        if not isinstance(self.service_context,dict) or not isinstance(eligible,list) or len(eligible)!=len(refs) or any(type(v) is not bool for v in eligible):raise ConversationFailure(503)
        self.service_eligible=tuple(eligible)
        if not isinstance(values,list) or len(values)!=len(refs) or any(type(v) is not bool for v in values):raise ConversationFailure(503)
        def valid():return type(deadline) in (int,float) and math.isfinite(deadline) and time.time()<min(deadline,self.service_gate.expires_at if self.service_gate is not None else deadline)
        if not valid():raise ConversationFailure(401)
        yield tuple(values)
        if not valid():raise ConversationFailure(401)
