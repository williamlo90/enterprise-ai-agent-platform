"""Publication never holds a database transaction across HTTP copy/ACK."""
from datetime import date
from hashlib import sha256
import json
import math
import time
from enterprise_ai_platform.modules.policies.domain import PolicyFailure,Package,ReceivedCopy,canonical_body,encoded,identifier,integer,digest,validate_ack,eligibility_response
from enterprise_ai_platform.modules.policies.ports import PolicyRepository,PackageReceiver,LockedClaimBindings
from enterprise_ai_platform.modules.identity.service import opaque,digest as identity_digest
from enterprise_ai_platform.modules.delegation.ports import DelegationServicePort,canonical_payload


class DenyReceiver:
    async def receive(self,*args):raise PolicyFailure(503)


class DenyLockedBindings:
    async def verify(self,*args):raise PolicyFailure(503)


class PolicyService:
    def __init__(self,repository:PolicyRepository,origin,delegation:DelegationServicePort=None,*,
                 receiver:PackageReceiver=None,locked:LockedClaimBindings=None,clock=time.time):
        self.repository,self.origin,self.delegation=repository,origin,delegation
        self.receiver,self.locked,self.clock=receiver or DenyReceiver(),locked or DenyLockedBindings(),clock

    def token(self,session):
        if not opaque(session):raise PolicyFailure(401)
        return identity_digest('session',session)

    def csrf(self,headers):
        if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise PolicyFailure(403)
        return identity_digest('csrf',headers['x-csrf-token'])

    async def call(self,token,operation,payload):
        result=await self.repository.call(token,operation,payload)
        deadline=result.get('authority_expires_at')
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise PolicyFailure(401)
        return result

    async def mutate(self,session,headers,operation,values):
        token=self.token(session);csrf=self.csrf(headers)
        if type(values) is not dict:raise PolicyFailure(400)
        values=dict(values)
        try:
            identifier(values['package_id']);integer(values['package_version']);identifier(values['key'])
            if operation=='draft':
                if set(values)!={'package_id','package_version','key','expected_version','content'}:raise ValueError
                integer(values['expected_version'],minimum=0)
                if type(values['content']) is not dict or 'digest' in values['content']:raise ValueError
                normalized=json.loads(canonical_body({**values['content'],'digest':'0'*64}));normalized.pop('digest')
                context=await self.call(token,'context',{})
                content=dict(tenant_id=context['tenant_id'],package_id=values['package_id'],package_version=values['package_version'],**normalized)
                # Domain digest construction is identical to the receiver. No
                # tenant identity is accepted from the request body.
                normalized['digest']=sha256(b'reimbursement:rule-package:v1:'+encoded(content)).hexdigest()
                package=Package(context['tenant_id'],values['package_id'],values['package_version'],canonical_body(normalized))
                values.pop('content');values.update(canonical=encoded(package.content).decode(),digest=package.digest)
            elif operation=='supersede':
                if set(values)!={'package_id','package_version','key','expected_version','old_window_id','old_window_version','tenant_version','cutoff'}:raise ValueError
                identifier(values['old_window_id']);integer(values['old_window_version']);integer(values['tenant_version'])
                if date.fromisoformat(values['cutoff']).isoformat()!=values['cutoff']:raise ValueError
                integer(values['expected_version'])
            else:
                if operation not in {'prepare','dispatch','activate','retire','withdraw','conflict'} or set(values)!={'package_id','package_version','key','expected_version'}:raise ValueError
                integer(values['expected_version'])
        except (ValueError,TypeError,KeyError):raise PolicyFailure(400) from None
        if operation!='dispatch':
            return await self.call(token,operation,{**values,'csrf_hash':csrf})
        prepared=await self.call(token,'dispatch',{**values,'csrf_hash':csrf})
        context={**prepared['publisher'],'expected_version':values['expected_version']};body=prepared['body']
        try:received=await self.receiver.receive(context,body,values['package_id'],values['package_version'],values['key'],prepared['operation_id'])
        except PolicyFailure:raise
        except Exception:raise PolicyFailure(503) from None
        if type(received) is not ReceivedCopy:raise PolicyFailure(503)
        ack=validate_ack(dict(received.ack),values['package_id'],values['package_version'],body['digest'])
        if type(received.key_id) is not str or not received.key_id or len(received.key_id)>64:raise PolicyFailure(503)
        try:digest(received.proof)
        except ValueError:raise PolicyFailure(503) from None
        if ack['received_at']>self.clock()+5:raise PolicyFailure(503)
        # ACK settlement requires current publisher/source authority too. A
        # remote copy after lost authority stays inactive, never autoactivated.
        return await self.call(token,'ack',{**values,'ack':ack,'settlement':dict(key_id=received.key_id,proof=received.proof),'csrf_hash':csrf})

    async def eligibility(self,credential,delegation,values):
        if self.delegation is None:raise PolicyFailure(503)
        try:
            if type(values) is not dict or set(values)!={'payload','policy','operation_kind','trip_date','claim_ref'}:raise ValueError
            policy=values['policy']
            if type(policy) is not dict or set(policy)!={'id','version','digest'}:raise ValueError
            identifier(policy['id']);integer(policy['version']);digest(policy['digest'])
            if date.fromisoformat(values['trip_date']).isoformat()!=values['trip_date']:raise ValueError
            kind=values['operation_kind'];payload=values['payload']
            if kind not in {'submission','information','decision'} or payload['action']!={'submission':'claim.submit','information':'claim.information','decision':'claim.decide'}[kind]:raise ValueError
            if kind=='submission':
                if values['claim_ref'] is not None or payload['data']['policy']!=policy:raise ValueError
            else:
                identifier(values['claim_ref'])
                if values['claim_ref']!=payload['object']['id']:raise ValueError
                if kind=='information' and payload['data']['policy']!=policy:raise ValueError
            values={**values,'payload':json.loads(canonical_payload(payload))}
        except (ValueError,TypeError,KeyError):raise PolicyFailure(400) from None
        verified=await self.delegation.verify_private(credential,delegation,values['payload']);actor=dict(verified.actor)
        if actor.get('allow') is not True or actor.get('scope')!='actor_only' or self.clock()>=actor['expires_at']:raise PolicyFailure(401)
        if values['operation_kind']!='submission':
            if (await self.locked.verify(actor,values['payload'],values['policy'],values['trip_date'],values['claim_ref'])) is not True:raise PolicyFailure(403)
        # Revalidate after the last dependency (which may await a trusted local
        # claim binding source); no callback into the service is permitted.
        verified=await self.delegation.verify_private(credential,delegation,values['payload']);actor=dict(verified.actor)
        if actor.get('allow') is not True or actor.get('scope')!='actor_only' or self.clock()>=actor['expires_at']:raise PolicyFailure(401)
        credential_hash=sha256(('eai:internal-caller:v1:'+credential).encode()).hexdigest()
        result=await self.repository.eligibility(actor,values,credential_hash,verified.reference.wire())
        return eligibility_response(result,actor,values,self.clock())
