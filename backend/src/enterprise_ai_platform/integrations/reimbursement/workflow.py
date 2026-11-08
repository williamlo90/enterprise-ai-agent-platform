from enterprise_ai_platform.integrations.security.tls import internal_tls
"""Fixed service routes and bounded grants from a currently leased ACT snapshot."""
import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from uuid import uuid4
import httpx
from enterprise_ai_platform.modules.flows.domain import FlowFailure,encoded,identifier
from enterprise_ai_platform.modules.delegation.ports import canonical_payload
from enterprise_ai_platform.integrations.reimbursement.policies import TransportLog


class WorkflowTransport:
    def __init__(self,target,signer,recovery_credential,*,transport=None,clock=time.time):
        if type(recovery_credential) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}',recovery_credential) or recovery_credential==target.dispatcher_credential:raise ValueError('Invalid recovery credential.')
        self.target,self.signer,self.recovery,self.clock=target,signer,recovery_credential,clock
        for name in ('httpx','httpcore','httpcore.connection','httpcore.http11','httpcore.http2'):
            log=logging.getLogger(name)
            if not any(isinstance(f,TransportLog) for f in log.filters):log.addFilter(TransportLog())
        self.http=httpx.AsyncClient(verify=internal_tls(),base_url=target.origin,transport=transport,trust_env=False,follow_redirects=False,timeout=httpx.Timeout(10,connect=2),limits=httpx.Limits(max_connections=2,max_keepalive_connections=2))

    async def close(self):await self.http.aclose()

    def grant(self,packet,*,read=False):
        ctx=packet['context'];identity=packet['identity'];now=int(self.clock())
        payload=packet['payload']
        receipt=None
        if read:
            payload=dict(schema_version=1,action='operation.read',tenant_id=ctx['tenant_id'],actor_id=ctx['actor_id'],operation_id=str(uuid4()),object=dict(kind='operation',id=identity['operation_id'],version=1),data={})
        else:
            receipt=packet['receipt'];now=max(now,receipt['human_action_time'])
            identifier(receipt['id'])
            if receipt['operation_id']!=identity['operation_id'] or receipt['session_id']!=ctx['session_id'] or receipt['revoked']:raise ValueError
        payload=json.loads(canonical_payload(payload));digest=hashlib.sha256(b'eai:human-action:v1:'+canonical_payload(payload)).hexdigest()
        if not read and (digest!=identity['request_hash'] or digest!=receipt['request_hash']):raise ValueError
        expiry=min(int(packet['expires_at']),int(self.clock())+60)
        if not self.clock()<expiry or expiry<=now:raise ValueError
        obj=payload['object'];claims=dict(iss=self.signer.issuer,aud=self.signer.audience,caller=self.signer.caller,sub=ctx['actor_id'],tenant_id=ctx['tenant_id'],membership_id=ctx['membership_id'],session_id=ctx['session_id'],access_revision=ctx['access_revision'],action=payload['action'],object_kind=obj['kind'],object_id=obj['id'],object_version=obj['version'],operation_id=payload['operation_id'],request_hash=digest,jti=str(uuid4()),iat=now,nbf=now,exp=expiry,human_receipt_id=None if read else receipt['id'])
        return payload,self.signer.sign(claims,now=int(self.clock()))

    async def request(self,method,path,packet,*,read=False,recover=False,seal=False):
        try:
            payload,token=self.grant(packet,read=read)
            headers={'authorization':'Bearer '+(self.recovery if recover else self.target.dispatcher_credential),'x-eai-delegation':token,'content-type':'application/json'}
            body=None
            if method=='GET':headers['x-eai-operation-payload']=base64.urlsafe_b64encode(encoded(payload)).decode().rstrip('=')
            elif seal:body=dict(payload=payload,identity=packet['identity'])
            else:body=dict(payload=payload,key=packet['identity']['key'])
            async with asyncio.timeout(12):
                async with self.http.stream(method,path,content=None if body is None else encoded(body),headers=headers) as response:
                    if response.status_code==404 and method=='GET':return None
                    if response.status_code!=200:raise FlowFailure('unknown')
                    if response.headers.get('content-type','').split(';')[0]!='application/json' or response.headers.get('content-encoding','identity')!='identity':raise ValueError
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw)+len(chunk)>32768:raise ValueError
                        raw.extend(chunk)
            def unique(rows):
                out={}
                for k,v in rows:
                    if k in out:raise ValueError
                    out[k]=v
                return out
            value=json.loads(bytes(raw),object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            if self.clock()>=packet['expires_at']:raise ValueError
            return value
        except Exception:raise FlowFailure('unknown') from None

    async def execute(self,packet):return await self.request('POST','/service/v1/claims/execute',packet)
    async def lookup(self,packet):return await self.request('GET','/service/v1/operations/'+packet['identity']['operation_id'],packet,read=True)
    async def seal(self,packet):return await self.request('POST','/service/v1/operations/'+packet['identity']['operation_id']+'/seal-if-absent',packet,read=True,recover=True,seal=True)
