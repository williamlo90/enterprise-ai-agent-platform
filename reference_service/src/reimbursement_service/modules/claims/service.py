"""Claim feature work joins the existing operation transaction, never network I/O.

This foundation does not enable an HTTP writer. Preparation with current human,
policy and receipt authority is composed separately and defaults to deny.
"""
import asyncio
from dataclasses import dataclass,field,replace
from datetime import datetime,timezone
import json
import math
import time
from uuid import uuid5,NAMESPACE_URL

from reimbursement_service.modules.claims.domain import (
    ClaimFailure,ClaimPermit,Financial,Item,ClaimState,integer,identifier,
    validate_financial,initial_state,decide,provide_information,
)
from reimbursement_service.modules.claims.ports import ClaimsRepository,ClaimsAuthority
from reimbursement_service.modules.operations.ports import Result,Identity
from reimbursement_service.modules.access.ports import VerifiedActor,canonical_payload,request_hash


def strict_packet(raw):
    def pairs(rows):
        result={}
        for key,value in rows:
            if key in result:raise ValueError
            result[key]=value
        return result
    def bad(_):raise ValueError
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=bad)


@dataclass(frozen=True)
class ClaimWork:
    repository: ClaimsRepository = field(repr=False)
    operation: str
    packet: bytes = field(repr=False)
    expected: Result
    clock: object = field(default=time.time,repr=False)

    def __post_init__(self):
        try:
            if self.operation not in {'submit','information','decision'} or type(self.packet) is not bytes or len(self.packet)>131072:raise ValueError
            data=strict_packet(self.packet)
            if type(data) is not dict or type(self.expected) is not Result or self.expected.ref!=data['claim_id']:raise ValueError
            integer(data['expires_at']);integer(self.expected.version)
            if not callable(self.clock):raise ValueError
            codes={'submit':{'claim_submitted'},'information':{'information_received'},
                   'decision':{'stage_approved','claim_approved','claim_rejected','information_requested'}}
            if self.expected.code not in codes[self.operation]:raise ValueError
        except (ValueError,TypeError,KeyError):raise ClaimFailure(400) from None

    async def apply(self,uow):
        packet=strict_packet(self.packet)
        if self.clock()>=packet['expires_at']:raise ClaimFailure(401)
        value=await self.repository.apply(uow,self.operation,packet)
        if self.clock()>=packet['expires_at']:raise ClaimFailure(401)
        if type(value) is not dict:raise ClaimFailure(503)
        if value.get('status')=='ok':
            if value.get('result')!=self.expected.wire():raise ClaimFailure(503)
            return 'committed',self.expected
        if value.get('status')=='conflict':
            code=value.get('code')
            if code not in {'business_duplicate','stale_claim','stale_stage','terminal_claim','deadline_passed','invalid_previous_claim','stale_draft','claim_exists'}:raise ClaimFailure(503)
            return 'rejected',Result(None,None,code)
        raise ClaimFailure({'invalid':400,'unauthenticated':401,'forbidden':403,'missing':404}.get(value.get('status'),503))


class DenyClaimsAuthority:
    async def authorize(self,*args,**kwargs):raise ClaimFailure(503)


def claim_state(value):
    """Private repository DTO → immutable domain snapshot, never client state."""
    try:
        policy=value['policy'];financial=value['financial']
        frozen=Financial(value['tenant_id'],value['owner_id'],value['trip_id'],policy['id'],policy['version'],policy['digest'],
            financial['currency'],financial['total_minor'],tuple(Item(i['id'],i['trip_item_id'],i['amount_minor'],tuple(i['receipt_ids'])) for i in value['items']),
            tuple((r['id'],r['version'],r['digest']) for r in value['receipts']),tuple(s['approver_id'] for s in value['stages']))
        if [s['stage'] for s in value['stages']]!=list(range(1,len(value['stages'])+1)):raise ValueError
        return ClaimState(value['id'],frozen,value['state'],value['version'],value['revision'],value['active_stage'],tuple(s['state'] for s in value['stages']))
    except (KeyError,TypeError,ValueError):raise ClaimFailure(503) from None


class ClaimsService:
    """Trusted preparation before a short REF-03 UoW; no public permit input.

    Dependencies return verified source evidence, not a user-controlled allow
    flag. Production providers are intentionally unbound until FILE/ACT/FLOW.
    """
    def __init__(self,repository:ClaimsRepository,operations,*,authority:ClaimsAuthority=None,clock=time.time,ids=None):
        self.repository,self.operations=repository,operations
        self.authority=DenyClaimsAuthority() if authority is None else authority
        self.clock,self.ids=clock,ids

    def binding(self,payload,actor):
        now=self.clock()
        if type(now) not in (int,float) or not math.isfinite(now) or type(actor) is not VerifiedActor:raise ClaimFailure(401)
        try:
            integer(actor.expires_at);integer(actor.verified_at);integer(actor.access_revision)
            identifier(actor.tenant_id);identifier(actor.actor_id);identifier(actor.membership_id)
        except (TypeError,ValueError):raise ClaimFailure(401) from None
        if actor.scope!='actor_only' or not now<actor.expires_at<=now+65 or actor.verified_at>now+5:raise ClaimFailure(401)
        try:canonical=canonical_payload(payload);frozen=json.loads(canonical);digest=request_hash(frozen)
        except (TypeError,ValueError,KeyError):raise ClaimFailure(400) from None
        if (actor.tenant_id,actor.actor_id,actor.action,actor.operation_id,actor.request_hash)!=(
                frozen['tenant_id'],frozen['actor_id'],frozen['action'],frozen['operation_id'],digest):raise ClaimFailure(403)
        return canonical,frozen,digest,now

    def view(self,value,claim_id,actor):
        fields={'tenant_id','id','owner_id','trip_id','trip_date','policy','state','version','revision','active_stage','reviewer_id'}
        if type(value) is not dict or not fields<=value.keys() or (value['tenant_id'],value['id'])!=(actor.tenant_id,claim_id):raise ClaimFailure(503)
        if value['owner_id']!=actor.actor_id and (value['state']!='pending_approval' or value['reviewer_id']!=actor.actor_id):raise ClaimFailure(503)
        return value

    async def read(self,payload,actor):
        _,frozen,_,_=self.binding(payload,actor)
        if frozen['action']!='claim.read':raise ClaimFailure(403)
        result=await self.repository.read(frozen['object']['id'],actor)
        self.binding(frozen,actor)
        return self.view(result,frozen['object']['id'],actor)

    async def relation(self,payload,actor):
        _,frozen,_,_=self.binding(payload,actor)
        if frozen['action'] not in {'claim.read','claim.information','claim.decide'}:raise ClaimFailure(403)
        result=await self.repository.relation(frozen['object']['id'],actor)
        self.binding(frozen,actor)
        return self.view(result,frozen['object']['id'],actor)

    async def access_check(self,payload,actor):
        """Current revision receipts for the owner/current active reviewer only.

        Uses the existing locked read capability, not the metadata-only relation
        DTO. No policy, pin or platform callback is needed to read this relation.
        """
        value=await self.read(payload,actor)
        try:
            integer(value['version']);integer(value['revision'])
            refs=value['receipts']
            if type(refs) is not list or len(refs)>10:raise ValueError
            result=[]
            for ref in refs:
                if type(ref) is not dict or set(ref)!={'id','version','digest'}:raise ValueError
                identifier(ref['id']);integer(ref['version'])
                if type(ref['digest']) is not str or len(ref['digest'])!=64 or any(c not in '0123456789abcdef' for c in ref['digest']):raise ValueError
                result.append(dict(ref))
            if len({r['id'] for r in result})!=len(result):raise ValueError
        except (KeyError,ValueError,TypeError):raise ClaimFailure(503) from None
        _,frozen,digest,now=self.binding(payload,actor)
        return dict(allow=True,tenant_id=actor.tenant_id,actor_id=actor.actor_id,claim_id=value['id'],
            claim_version=value['version'],revision=value['revision'],receipts=sorted(result,key=lambda r:r['id']),
            operation_id=frozen['operation_id'],request_hash=digest,checked_at=int(now),expires_at=actor.expires_at)

    async def execute(self,payload,key,actor,*,context=None):
        canonical,frozen,digest,received=self.binding(payload,actor)
        action=frozen['action'];data=frozen['data']
        if action not in {'claim.submit','claim.information','claim.decide'}:raise ClaimFailure(403)
        try:identifier(key)
        except (ValueError,TypeError):raise ClaimFailure(400) from None
        # Immutable prior outcomes are returned before recalculating deadlines or
        # making new external calls. A concurrent commit is checked again in UoW.
        existing=await self.operations.replay(frozen,key,actor)
        if existing is not None:return existing
        self.binding(frozen,actor)
        operation={'claim.submit':'submit','claim.information':'information','claim.decide':'decision'}[action]
        claim=None;previous=None
        if operation!='submit':
            claim=self.view(await self.repository.read(frozen['object']['id'],actor),frozen['object']['id'],actor)
            current=claim_state(claim)
            if current.id!=frozen['object']['id'] or current.financial.tenant_id!=actor.tenant_id:raise ClaimFailure(503)
        if operation in {'submit','information'}:
            if operation=='information' and (data['trip_id']!=current.financial.trip_id or data['policy']!={'id':current.financial.policy_id,'version':current.financial.policy_version,'digest':current.financial.policy_digest}):raise ClaimFailure(409)
            facts=await self.repository.facts(data['trip_id'],data['policy'],actor,item_ids=tuple(i['trip_item_id'] for i in data['items']))
            financial=validate_financial(data,tenant_id=actor.tenant_id,owner_id=actor.actor_id,trip=facts['trip'],policy=facts['policy'],
                received_at=datetime.fromtimestamp(received,timezone.utc),initial=operation=='submit')
            policy={'id':financial.policy_id,'version':financial.policy_version,'digest':financial.policy_digest}
            trip_date=facts['trip']['end_date']
            if operation=='submit':
                # A lost pin response can precede any ledger record. Derive a
                # stable private candidate from the complete verified operation
                # identity so another process/retry asks for the SAME pin batch.
                candidate=self.ids() if self.ids is not None else str(uuid5(NAMESPACE_URL,
                    'urn:enterprise-ai:claim-candidate:v1:'+actor.tenant_id+':'+actor.operation_id+':'+key+':'+digest))
                try:next_state=initial_state(candidate,financial)
                except (ValueError,TypeError):raise ClaimFailure(503) from None
                previous=facts.get('previous_claim_id')
                if previous is not None:
                    try:identifier(previous)
                    except (TypeError,ValueError):raise ClaimFailure(503) from None
                code='claim_submitted'
            else:
                next_state=provide_information(current,financial,actor_id=actor.actor_id,expected_version=frozen['object']['version'],revision=data['revision'])
                code='information_received'
        else:
            next_state=decide(current,actor_id=actor.actor_id,expected_version=frozen['object']['version'],revision=data['revision'],
                stage=data['stage'],decision=data['decision'],reason=data['reason'])
            policy={'id':current.financial.policy_id,'version':current.financial.policy_version,'digest':current.financial.policy_digest}
            trip_date=claim['trip_date']
            code={'approved':'claim_approved','rejected':'claim_rejected','needs_information':'information_requested','pending_approval':'stage_approved'}[next_state.state]
        # The dependency receives copies; it cannot mutate the frozen command or
        # the expected source identity that will be checked against its response.
        policy_wire=dict(policy)
        metadata=None if claim is None else {k:claim[k] for k in ('tenant_id','id','owner_id','trip_id','trip_date','policy','state','version','revision','active_stage','reviewer_id')}
        self.binding(frozen,actor)
        try:
            async with asyncio.timeout(min(10,actor.expires_at-self.clock())):
                permit=await self.authority.authorize(json.loads(canonical),actor,operation_key=key,candidate_claim_id=next_state.id,policy=dict(policy_wire),trip_date=trip_date,
                    claim=json.loads(json.dumps(metadata)),context=context)
        except ClaimFailure:raise
        except Exception:raise ClaimFailure(503) from None
        self.binding(frozen,actor)
        if type(permit) is not ClaimPermit:raise ClaimFailure(503)
        evidence=permit.checked(request_hash=digest,operation_id=frozen['operation_id'],operation_key=key,candidate_claim_id=next_state.id,policy=policy_wire,
            operation_kind={'submit':'submission','information':'information','decision':'decision'}[operation],trip_date=trip_date,
            claim_ref=None if operation=='submit' else current.id,receipts=frozen['data'].get('receipts',[]),actor_expires_at=actor.expires_at,now=self.clock())
        # The same bounded actor goes through ledger lock, feature effects,
        # ledger/audit finish AND final transaction expiry/commit validation.
        bounded=replace(actor,expires_at=evidence['expires_at'])
        identity=Identity(actor.tenant_id,actor.operation_id,actor.actor_id,'platform-dispatcher',action,key,digest,
            frozen['object']['kind'],frozen['object']['id'],frozen['object']['version'])
        packet=dict(identity=identity.wire(),canonical=canonical.decode(),permit=evidence,claim_id=next_state.id,
            received_at=datetime.fromtimestamp(received,timezone.utc).isoformat(),previous_claim_id=previous,expires_at=bounded.expires_at)
        work=ClaimWork(self.repository,operation,json.dumps(packet,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode(),
            Result(next_state.id,next_state.version,code),self.clock)
        return await self.operations.commit(frozen,key,bounded,work)
