from contextlib import asynccontextmanager
import math
import time
from uuid import uuid4
from enterprise_ai_platform.modules.conversations.domain import ConversationFailure,identifier,bounded_text
from enterprise_ai_platform.modules.identity.service import digest,opaque

class DenySources:
    @asynccontextmanager
    async def hold(self,subject,sources):yield tuple(False for _ in sources)

class ConversationService:
    def __init__(self,repository,origin,*,sources=None,clock=time.time):
        self.repository,self.origin,self.sources,self.clock=repository,origin,sources or DenySources(),clock
    def token(self,session):
        if not opaque(session):raise ConversationFailure(401)
        return digest('session',session)
    def csrf(self,headers):
        if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise ConversationFailure(403)
        return digest('csrf',headers['x-csrf-token'])
    def checked(self,row):
        deadline=row.pop('authority_expires_at',None)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise ConversationFailure(401)
        row.pop('tenant_id',None);row.pop('principal_id',None)
        return row
    async def call(self,session,operation,payload):return self.checked(await self.repository.call(self.token(session),operation,payload))
    async def create(self,session,headers,values):
        if not isinstance(values,dict) or set(values)!={'language','key'} or values['language'] not in ('id','en'):raise ConversationFailure(400)
        identifier(values['key'])
        return await self.call(session,'create',{**values,'id':str(uuid4()),'csrf_hash':self.csrf(headers)})
    async def list(self,session,after=None,limit=10):
        if after is not None:identifier(after)
        if type(limit) is not int or not 1<=limit<=20:raise ConversationFailure(400)
        return await self.call(session,'list',dict(after=after,limit=limit))
    async def rename(self,session,headers,conversation_id,values):
        identifier(conversation_id)
        if not isinstance(values,dict) or set(values)!={'name','expected_version','key'}:raise ConversationFailure(400)
        identifier(values['key']);bounded_text(values['name'],4096,8192)
        if type(values['expected_version']) is not int or not 1<=values['expected_version']<10**18:raise ConversationFailure(400)
        return await self.call(session,'rename',{**values,'id':conversation_id,'csrf_hash':self.csrf(headers)})
    async def append(self,session,headers,conversation_id,values):
        identifier(conversation_id)
        if not isinstance(values,dict) or set(values)!={'text','client_message_id'}:raise ConversationFailure(400)
        identifier(values['client_message_id']);bounded_text(values['text'],4096,8192)
        return await self.call(session,'append',{**values,'id':conversation_id,'message_id':str(uuid4()),'run_id':str(uuid4()),'csrf_hash':self.csrf(headers)})
    async def assisted(self,session,headers,conversation_id,values):
        from enterprise_ai_platform.modules.assisted.ports import selection
        identifier(conversation_id)
        if not isinstance(values,dict) or set(values)!={'text','client_message_id','selection'}:raise ConversationFailure(400)
        identifier(values['client_message_id']);bounded_text(values['text'],4096,8192)
        try:selection(values['selection'])
        except ValueError:raise ConversationFailure(400) from None
        return await self.call(session,'assisted_append',{**values,'id':conversation_id,'message_id':str(uuid4()),'run_id':str(uuid4()),'csrf_hash':self.csrf(headers)})

    async def assisted_read(self,session,run_id):
        identifier(run_id)
        return await self.call(session,'assisted_read',dict(id=run_id))

    async def history(self,session,conversation_id,after=0,limit=20):
        identifier(conversation_id)
        if type(after) is not int or not 0<=after<10**18 or type(limit) is not int or not 1<=limit<=20:raise ConversationFailure(400)
        return self.checked(await self.repository.history(self.token(session),dict(id=conversation_id,after=after,limit=limit),self.sources))
    async def feedback(self,session,headers,message_id,values):
        identifier(message_id)
        if not isinstance(values,dict) or set(values)!={'key','rating','reason'} or values['rating'] not in ('up','down'):raise ConversationFailure(400)
        identifier(values['key'])
        if values['reason'] is not None:bounded_text(values['reason'],1000,4096,empty=True)
        return await self.call(session,'feedback',{**values,'id':message_id,'csrf_hash':self.csrf(headers)})

    async def run(self,session,run_id,*,headers=None,expected_version=None):
        identifier(run_id)
        payload=dict(token=self.token(session),id=run_id)
        operation='read'
        if headers is not None:
            if type(expected_version) is not int or not 1<=expected_version<10**18:raise ConversationFailure(400)
            payload.update(csrf_hash=self.csrf(headers),expected_version=expected_version,job_id=str(uuid4()))
            operation='resume'
        return self.checked(await self.repository.run(operation,payload,self.sources))

    async def diagnostics(self,session,run_id):
        identifier(run_id)
        return self.checked(await self.repository.run('diagnostics',dict(token=self.token(session),id=run_id),self.sources))

    async def recover(self,session,headers,run_id,expected_version):
        identifier(run_id)
        if type(expected_version) is not int or not 1<=expected_version<10**18:raise ConversationFailure(400)
        return self.checked(await self.repository.run('recover',dict(token=self.token(session),id=run_id,expected_version=expected_version,csrf_hash=self.csrf(headers)),self.sources))
