"""One manual/agent draft path. Preview never issues consent or a transaction."""
import math
import time
from uuid import NAMESPACE_URL,uuid5
from enterprise_ai_platform.modules.drafts.domain import DraftFailure,VerifiedDraftSource,exact,identifier,integer,editable
from enterprise_ai_platform.modules.drafts.ports import DraftRepository,DraftSources
from enterprise_ai_platform.modules.identity.service import opaque,digest
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure


class DenyDraftSources:
    async def read(self,*args):raise DraftFailure(503)


class DraftService:
    def __init__(self,repository:DraftRepository,origin:str,source:DraftSources=None,*,clock=time.time):
        self.repository,self.origin,self.source,self.clock=repository,origin,source or DenyDraftSources(),clock

    def token(self,session):
        if not opaque(session):raise DraftFailure(401)
        return digest('session',session)

    def csrf(self,headers):
        if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise DraftFailure(403)
        return digest('csrf',headers['x-csrf-token'])

    async def call(self,token,operation,payload):
        value=await self.repository.call(token,operation,payload)
        expires=value.get('authority_expires_at')
        if type(expires) not in (int,float) or not math.isfinite(expires) or self.clock()>=expires:raise DraftFailure(401)
        return value

    def output(self,value,key='draft'):
        result=dict(value[key]);result['_authority_expires_at']=int(value['authority_expires_at'])
        return result

    async def create(self,session,headers,values):
        token=self.token(session);csrf=self.csrf(headers)
        try:
            exact(values,{'kind','trip_id','claim_id','conversation_id','key'})
            identifier(values['trip_id']);identifier(values['key'])
            if values['kind'] not in {'submission','information_update'}:raise ValueError
            if values['kind']=='submission' and values['claim_id'] is not None:raise ValueError
            if values['kind']=='information_update':identifier(values['claim_id'])
            if values['conversation_id'] is not None:identifier(values['conversation_id'])
            values=dict(values,csrf_hash=csrf,source=None)
        except (ValueError,TypeError,KeyError):raise DraftFailure(400) from None
        initial=await self.call(token,'create',values)
        if initial['draft'] is not None:return self.output(initial)
        context=dict(initial['context'])
        try:
            source=await self.source.read(dict(context),values['trip_id'],values['claim_id'])
            if type(source) is not VerifiedDraftSource:raise DraftFailure(503)
            attestation=source.wire()
        except (DraftFailure,BrowserContextFailure):raise
        except Exception:raise DraftFailure(503) from None
        candidate=str(uuid5(NAMESPACE_URL,'urn:enterprise-ai:draft:v1:'+context['tenant_id']+':'+context['actor_id']+':'+values['key']))
        result=await self.call(token,'create',{**values,'source':attestation,'id':candidate})
        return self.output(result)

    async def read(self,session,draft_id):
        try:identifier(draft_id)
        except ValueError:raise DraftFailure(400) from None
        return self.output(await self.call(self.token(session),'read',dict(id=draft_id)))

    async def replace(self,session,headers,draft_id,values):
        try:
            identifier(draft_id);exact(values,{'expected_version','key','content'})
            integer(values['expected_version']);identifier(values['key'])
            content=editable(values['content'])
        except (ValueError,TypeError,KeyError):raise DraftFailure(400) from None
        result=await self.call(self.token(session),'replace',dict(id=draft_id,expected_version=values['expected_version'],key=values['key'],content=content,csrf_hash=self.csrf(headers)))
        return self.output(result)

    async def preview(self,session,headers,draft_id,values):
        try:
            identifier(draft_id);exact(values,{'expected_version'});integer(values['expected_version'])
        except (ValueError,TypeError,KeyError):raise DraftFailure(400) from None
        token=self.token(session);csrf=self.csrf(headers)
        initial=await self.call(token,'read',dict(id=draft_id));draft=initial['draft']
        if draft['state']!='editable' or draft['version']!=values['expected_version']:raise DraftFailure(409)
        try:
            source=await self.source.read(dict(initial['context']),draft['trip_id'],draft['claim_id'])
            if type(source) is not VerifiedDraftSource:raise DraftFailure(503)
            attestation=source.wire()
        except (DraftFailure,BrowserContextFailure):raise
        except Exception:raise DraftFailure(503) from None
        result=await self.call(token,'preview',dict(id=draft_id,expected_version=values['expected_version'],source=attestation,csrf_hash=csrf))
        return self.output(result,'preview')
