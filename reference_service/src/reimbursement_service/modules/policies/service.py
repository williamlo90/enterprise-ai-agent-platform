import time
from reimbursement_service.modules.policies.ports import PackagesRepository,Package,canonical_body,request_digest
from reimbursement_service.modules.access.ports import ScopedAuthorityPort,CatalogAuthority,CatalogBinding,AccessFailure,FailureKind


class PackagesService:
    def __init__(self,repository:PackagesRepository,scopes:ScopedAuthorityPort,*,clock=time.time):
        self.repository,self.scopes,self.clock=repository,scopes,clock

    async def receive(self,headers,package_id,version,key,operation_id,body):
        try:
            canonical=canonical_body(body)
            binding=CatalogBinding('policy.publish',package_id,version,request_digest(package_id,version,canonical),operation_id,key)
        except (ValueError,TypeError,KeyError):raise AccessFailure(FailureKind.INVALID) from None
        authority=await self.scopes.authorize(headers,binding)
        if (type(authority) is not CatalogAuthority or authority.binding!=binding
                or authority.verified_at>self.clock()+5):raise AccessFailure(FailureKind.FORBIDDEN)
        if self.clock()>=authority.expires_at:raise AccessFailure(FailureKind.UNAVAILABLE)
        try:package=Package(authority.tenant_id,package_id,version,canonical)
        except (ValueError,TypeError):raise AccessFailure(FailureKind.CONFLICT) from None
        value=await self.repository.receive(package,key,authority)
        if self.clock()>=authority.expires_at:raise AccessFailure(FailureKind.UNAVAILABLE)
        return value,authority.expires_at
