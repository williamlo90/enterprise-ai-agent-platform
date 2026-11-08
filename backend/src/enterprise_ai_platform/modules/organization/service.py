import math
import time
from enterprise_ai_platform.modules.organization.domain import OrganizationFailure,identifier,label
from enterprise_ai_platform.modules.organization.ports import OrganizationRepository
from enterprise_ai_platform.modules.identity.service import digest,opaque

class OrganizationService:
    def __init__(self,repository:OrganizationRepository,origin:str,*,clock=time.time):self.repository,self.origin,self.clock=repository,origin,clock
    async def call(self,session,operation,payload):
        if not opaque(session):raise OrganizationFailure(401)
        row=await self.repository.call(digest('session',session),operation,payload)
        deadline=row.pop('authority_expires_at',None)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise OrganizationFailure(401)
        return row
    def page(self,after,limit,*,facet=False):
        if after is not None:
            if facet:after=label(after)
            else:identifier(after)
        if type(limit) is not int or not 1<=limit<=50:raise OrganizationFailure(400)
        return dict(after=after,limit=limit)
    def filters(self,*,mode,collection=None,query=None,department=None,job_title=None,group=None,after=None,limit=20,facet=False):
        if mode not in ('admin','picker') or mode=='admin' and collection is not None:raise OrganizationFailure(400)
        if collection is not None and collection!='tenant':identifier(collection)
        return dict(mode=mode,collection_id=None if collection=='tenant' else collection,query=label(query,200,800),department=label(department),job_title=label(job_title),group=label(group),**self.page(after,limit,facet=facet))
    async def directory(self,session,*,mode,**filters):
        return await self.call(session,'directory',self.filters(mode=mode,**filters))
    async def facets(self,session,*,mode,field,**filters):
        if field not in ('department','job_title','group'):raise OrganizationFailure(400)
        return await self.call(session,'facets',dict(field=field,**self.filters(mode=mode,facet=True,**filters)))
    async def collections(self,session,*,after=None,limit=20):
        return await self.call(session,'collections',self.page(after,limit))
    async def member(self,session,membership_id):
        return await self.call(session,'member',dict(id=identifier(membership_id)))
    async def replace(self,session,headers,membership_id,values):
        identifier(membership_id)
        if not isinstance(values,dict) or set(values)!={'display_name','department','job_title','groups','expected_version','key'}:raise OrganizationFailure(400)
        identifier(values['key'])
        if type(values['expected_version']) is not int or not 1<=values['expected_version']<10**18:raise OrganizationFailure(400)
        if not isinstance(values['groups'],list) or len(values['groups'])>32 or any(not isinstance(v,str) for v in values['groups']):raise OrganizationFailure(400)
        groups=sorted({label(value) for value in values['groups']},key=lambda value:value.encode('utf-8'))
        payload=dict(id=membership_id,key=values['key'],expected_version=values['expected_version'],display_name=label(values['display_name'],200,800),department=label(values['department']),job_title=label(values['job_title']),groups=groups)
        if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise OrganizationFailure(403)
        payload['csrf_hash']=digest('csrf',headers['x-csrf-token'])
        return await self.call(session,'replace',payload)
