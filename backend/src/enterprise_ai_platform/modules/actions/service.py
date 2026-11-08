import math
import time
from enterprise_ai_platform.modules.actions.domain import ActionFailure,VerifiedDecisionSource,exact,identifier,integer,digest,identifiers
from enterprise_ai_platform.modules.actions.ports import ActionRepository,DecisionSources
from enterprise_ai_platform.modules.drafts.ports import DraftSources,VerifiedDraftSource
from enterprise_ai_platform.modules.identity.service import opaque,digest as secret_digest
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure


class ActionService:
    def __init__(self,repository:ActionRepository,origin:str,drafts:DraftSources,decisions:DecisionSources,*,clock=time.time):
        self.repository,self.origin,self.drafts,self.decisions,self.clock=repository,origin,drafts,decisions,clock

    def token(self,session):
        if not opaque(session):raise ActionFailure(401)
        return secret_digest('session',session)

    def csrf(self,headers):
        if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise ActionFailure(403)
        return secret_digest('csrf',headers['x-csrf-token'])

    async def call(self,token,operation,values):
        result=await self.repository.call(token,operation,values);expiry=result.get('authority_expires_at')
        if type(expiry) not in (int,float) or not math.isfinite(expiry) or self.clock()>=expiry:raise ActionFailure(401)
        return result

    def output(self,result):return {**result['action'],'_authority_expires_at':int(result['authority_expires_at'])}

    async def confirm(self,session,headers,draft_id,values):
        token=self.token(session);csrf=self.csrf(headers)
        try:
            identifier(draft_id);exact(values,{'expected_version','material_hash','key'})
            integer(values['expected_version']);digest(values['material_hash']);identifier(values['key'])
        except (ValueError,TypeError,KeyError):raise ActionFailure(400) from None
        values=dict(values,id=draft_id,csrf_hash=csrf,source=None)
        initial=await self.call(token,'confirm',values)
        if initial.get('action') is not None:return self.output(initial)
        context=dict(initial['context']);draft=initial['draft']
        try:
            source=await self.drafts.read(dict(context),draft['trip_id'],draft['claim_id'])
            if type(source) is not VerifiedDraftSource:raise ActionFailure(503)
            attestation=source.wire()
        except (ActionFailure,BrowserContextFailure):raise
        except Exception as error:
            raise ActionFailure(getattr(error,'status',503)) from None
        result=await self.call(token,'confirm',{**values,**identifiers(context,values['key'],'confirm'),'source':attestation})
        return self.output(result)

    async def decide(self,session,headers,claim_id,values):
        token=self.token(session);csrf=self.csrf(headers)
        try:
            identifier(claim_id);exact(values,{'expected_version','revision','stage','decision','reason','key'})
            integer(values['expected_version']);integer(values['revision']);integer(values['stage'],10);identifier(values['key'])
            if values['decision'] not in {'approve','reject','request_information'} or type(values['reason']) is not str or len(values['reason'].encode('utf-8'))>4096 or '\x00' in values['reason'] or (values['decision']!='approve' and not values['reason'].strip()):raise ValueError
        except (ValueError,TypeError,KeyError,UnicodeError):raise ActionFailure(400) from None
        values=dict(values,id=claim_id,csrf_hash=csrf,source=None)
        initial=await self.call(token,'decision',values)
        if initial.get('action') is not None:return self.output(initial)
        context=dict(initial['context'])
        try:
            source=await self.decisions.read(dict(context),claim_id)
            if type(source) is not VerifiedDecisionSource:raise ActionFailure(503)
            attestation=source.wire()
        except (ActionFailure,BrowserContextFailure):raise
        except Exception as error:raise ActionFailure(getattr(error,'status',503)) from None
        return self.output(await self.call(token,'decision',{**values,**identifiers(context,values['key'],'decision'),'source':attestation}))

    async def read(self,session,operation_id):
        try:identifier(operation_id)
        except ValueError:raise ActionFailure(400) from None
        return self.output(await self.call(self.token(session),'read',dict(id=operation_id)))
