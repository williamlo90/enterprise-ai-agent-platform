import math
import time
from enterprise_ai_platform.modules.administration.domain import mutation,page,identifier,integer,exact
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.modules.identity.service import opaque,digest

class AdministrationService:
    def __init__(self,repository,origin,*,clock=time.time):self.repository,self.origin,self.clock=repository,origin,clock
    def token(self,session):
        if not opaque(session):raise AccessFailure(401)
        return digest('session',session)
    async def call(self,session,op,values):
        row=await self.repository.call(self.token(session),op,values);expiry=row.pop('authority_expires_at',None)
        if type(expiry) not in (int,float) or not math.isfinite(expiry) or self.clock()>=expiry:raise AccessFailure(401)
        return dict(row,_administration_deadline=int(expiry))
    async def read(self,session,operation,values):
        try:
            if operation in {'members','policies','tenants','jobs','audit','scopes'}:
                exact(values,{'after','limit'});page(values['after'],values['limit'],policy=operation=='policies')
            elif operation in {'member','policy'}:
                exact(values,{'id'} if operation=='member' else {'id','package_version'});identifier(values['id'])
                if operation=='policy':integer(values['package_version'])
            elif operation in {'config','catalog','profile'}:exact(values,set())
            else:raise ValueError
        except (ValueError,TypeError,KeyError):raise AccessFailure(400) from None
        return await self.call(session,operation,values)
    async def mutate(self,session,headers,operation,values):
        if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise AccessFailure(403)
        try:mutation(operation,values)
        except (ValueError,TypeError,KeyError):raise AccessFailure(400) from None
        return await self.call(session,operation,dict(values,csrf_hash=digest('csrf',headers['x-csrf-token'])))
