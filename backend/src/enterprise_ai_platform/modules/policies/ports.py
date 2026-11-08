from typing import Protocol
from enterprise_ai_platform.modules.policies.domain import PolicyFailure,ReceivedCopy


class PolicyRepository(Protocol):
    async def call(self,token,operation,payload)->dict: ...
    async def eligibility(self,actor,request,credential_hash,verification_ref)->dict: ...


class PackageReceiver(Protocol):
    async def receive(self,context,body,package_id,version,key,operation_id)->ReceivedCopy: ...


class LockedClaimBindings(Protocol):
    async def verify(self,actor,payload,policy,trip_date,claim_ref)->bool: ...


class PolicyPort(Protocol):
    async def mutate(self,session,headers,operation,values)->dict: ...
    async def eligibility(self,credential,delegation,values)->dict: ...
