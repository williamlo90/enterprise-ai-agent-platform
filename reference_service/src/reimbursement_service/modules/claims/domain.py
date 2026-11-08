"""Deterministic financial/deadline/state rules. No provider or database access."""
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
import json
import re
from uuid import UUID
from zoneinfo import ZoneInfo


class ClaimFailure(RuntimeError):
    def __init__(self, status=409):
        self.status = status if status in {400,401,403,404,409,503} else 503
        super().__init__('Claim operation rejected.')


def identifier(value):
    if type(value) is not str or str(UUID(value)) != value or UUID(value).int == 0:
        raise ValueError('Invalid claim reference.')
    return value


def integer(value, *, minimum=1, maximum=9223372036854775807):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid claim integer.')
    return value


def business_date(value):
    if type(value) is not str or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Invalid claim business date.')
    return date.fromisoformat(value)


def deadline_exclusive(trip_end, days, zone):
    """First instant AFTER the final permissible tenant calendar day."""
    end = business_date(trip_end)
    integer(days, minimum=0, maximum=3650)
    return datetime.combine(end + timedelta(days=days+1), time.min, ZoneInfo(zone)).astimezone(timezone.utc)


@dataclass(frozen=True)
class Item:
    line_id: str
    trip_item_id: str
    amount_minor: int
    receipt_ids: tuple[str, ...] = field(repr=False)

    def __post_init__(self):
        identifier(self.line_id); identifier(self.trip_item_id); integer(self.amount_minor)
        if type(self.receipt_ids) is not tuple or len(self.receipt_ids)>10 or len(set(self.receipt_ids))!=len(self.receipt_ids):
            raise ValueError('Invalid claim receipt references.')
        for value in self.receipt_ids: identifier(value)


@dataclass(frozen=True)
class Financial:
    tenant_id: str
    owner_id: str
    trip_id: str
    policy_id: str
    policy_version: int
    policy_digest: str = field(repr=False)
    currency: str
    total_minor: int
    items: tuple[Item, ...] = field(repr=False)
    receipts: tuple[tuple[str,int,str], ...] = field(repr=False)
    approvers: tuple[str, ...] = field(repr=False)

    def __post_init__(self):
        for value in (self.tenant_id,self.owner_id,self.trip_id,self.policy_id):identifier(value)
        integer(self.policy_version);integer(self.total_minor)
        if type(self.policy_digest) is not str or not re.fullmatch('[a-f0-9]{64}',self.policy_digest):raise ValueError('Invalid locked policy digest.')
        if self.currency not in {'IDR','SGD','USD'}:raise ValueError('Invalid claim currency.')
        if type(self.items) is not tuple or not 1<=len(self.items)<=100 or any(type(i) is not Item for i in self.items):raise ValueError('Invalid claim items.')
        if len({i.line_id for i in self.items})!=len(self.items) or len({i.trip_item_id for i in self.items})!=len(self.items):raise ValueError('Duplicate expense identity.')
        if sum(i.amount_minor for i in self.items)!=self.total_minor:raise ValueError('Claim total mismatch.')
        if type(self.receipts) is not tuple or len(self.receipts)>10:raise ValueError('Invalid claim receipt set.')
        ids=[]
        for row in self.receipts:
            if type(row) is not tuple or len(row)!=3:raise ValueError('Invalid claim receipt.')
            identifier(row[0]);integer(row[1])
            if type(row[2]) is not str or not re.fullmatch('[a-f0-9]{64}',row[2]):raise ValueError('Invalid receipt digest.')
            ids.append(row[0])
        if len(set(ids))!=len(ids) or set(ids)!={r for item in self.items for r in item.receipt_ids}:raise ValueError('Receipt set mismatch.')
        if type(self.approvers) is not tuple or not 1<=len(self.approvers)<=10:raise ValueError('Invalid approval route.')
        for value in self.approvers:
            identifier(value)
            if value==self.owner_id:raise ValueError('Self approval is not allowed.')

    @property
    def expense_ids(self):return frozenset(i.trip_item_id for i in self.items)

    @property
    def locked_policy(self):return self.policy_id,self.policy_version,self.policy_digest


def validate_financial(data, *, tenant_id, owner_id, trip, policy, received_at, initial=True):
    """Source facts come from trusted local trip/package repositories, not chat.

    Authority/receipt/eligibility attestations remain separate service gates.
    Information updates reuse locked rules without a new submission deadline.
    """
    try:
        if type(initial) is not bool or not isinstance(received_at,datetime) or received_at.tzinfo is None:raise ValueError
        if type(data) is not dict or type(trip) is not dict or type(policy) is not dict:raise ValueError
        fields={'trip_id','currency','items','total_minor','receipts','policy','approval_route','destination'}
        if not initial:fields|={'revision','draft'}
        if set(data)!=fields or data['destination']!='reference-reimbursement':raise ValueError
        if (trip['tenant_id']!=tenant_id or trip['owner_principal_ref']!=owner_id or trip['id']!=data['trip_id']
                or trip['state']!='completed' or policy['tenant_id']!=tenant_id):raise ClaimFailure(404)
        locked={'id':policy['package_id'],'version':policy['package_version'],'digest':policy['digest']}
        if type(data['policy']) is not dict or set(data['policy'])!={'id','version','digest'}:raise ValueError
        identifier(data['policy']['id']);integer(data['policy']['version'])
        if type(data['policy']['digest']) is not str or not re.fullmatch('[a-f0-9]{64}',data['policy']['digest']):raise ValueError
        if data['policy']!=locked:raise ClaimFailure(409)
        end=business_date(trip['end_date'])
        if end<business_date(policy['valid_trip_end_from']) or (policy['valid_trip_end_to'] is not None and end>=business_date(policy['valid_trip_end_to'])):raise ClaimFailure(409)
        rules=policy['rules'];route=data['approval_route']
        if type(route) is not list or not 1<=len(route)<=10 or route!=rules['approval_route']:raise ClaimFailure(409)
        for ordinal,stage in enumerate(route,1):
            if type(stage) is not dict or set(stage)!={'stage','approver_id','scope_ref'} or type(stage['stage']) is not int or stage['stage']!=ordinal or stage['scope_ref']!='reimbursement':raise ValueError
        if data['currency']!=rules['currency']:raise ValueError
        source={v['id']:v for v in trip['items']};categories={v['code']:v for v in rules['categories']}
        if type(data['items']) is not list or not 1<=len(data['items'])<=100 or type(data['receipts']) is not list or len(data['receipts'])>10:raise ValueError
        items=[]
        for row in data['items']:
            if type(row) is not dict or set(row)!={'id','trip_item_id','amount_minor','receipt_ids'} or type(row['receipt_ids']) is not list:raise ValueError
            item=Item(row['id'],row['trip_item_id'],row['amount_minor'],tuple(row['receipt_ids']))
            actual=source.get(item.trip_item_id)
            if actual is None:raise ClaimFailure(404)
            category=categories.get(actual['category'])
            if category is None or actual['currency']!=data['currency']:raise ValueError
            if category['receipt_required'] and not item.receipt_ids:raise ValueError
            if category['max_amount_minor'] is not None and item.amount_minor>category['max_amount_minor']:raise ValueError
            items.append(item)
        receipts=[]
        for row in data['receipts']:
            if type(row) is not dict or set(row)!={'id','version','digest'}:raise ValueError
            receipts.append((row['id'],row['version'],row['digest']))
        result=Financial(tenant_id,owner_id,trip['id'],locked['id'],locked['version'],locked['digest'],
            data['currency'],data['total_minor'],tuple(items),tuple(receipts),tuple(s['approver_id'] for s in route))
        if rules['max_total_minor'] is not None and result.total_minor>rules['max_total_minor']:raise ValueError
        if initial and received_at>=deadline_exclusive(trip['end_date'],rules['submission_deadline_days'],policy['timezone']):raise ClaimFailure(409)
        if not initial:
            integer(data['revision'])
            if type(data['draft']) is not dict or set(data['draft'])!={'id','version'}:raise ValueError
            identifier(data['draft']['id']);integer(data['draft']['version'])
        return result
    except ClaimFailure:raise
    except (ValueError,TypeError,KeyError,OverflowError):raise ClaimFailure(400) from None


@dataclass(frozen=True)
class ClaimState:
    id: str
    financial: Financial = field(repr=False)
    state: str
    version: int
    revision: int
    active_stage: int|None
    stages: tuple[str,...]

    def __post_init__(self):
        identifier(self.id);integer(self.version);integer(self.revision)
        if type(self.financial) is not Financial or type(self.stages) is not tuple or len(self.stages)!=len(self.financial.approvers):raise ValueError('Invalid claim stages.')
        if any(s not in {'pending','approved','rejected','information_requested','void'} for s in self.stages):raise ValueError('Invalid claim stage state.')
        if self.state=='pending_approval':
            integer(self.active_stage,maximum=len(self.stages))
            if any(s!='approved' for s in self.stages[:self.active_stage-1]) or any(s!='pending' for s in self.stages[self.active_stage-1:]):raise ValueError('Invalid active stage.')
        elif self.state in {'needs_information','approved','rejected'}:
            if self.active_stage is not None:raise ValueError('Inactive stage must be null.')
            if self.state=='approved' and any(s!='approved' for s in self.stages):raise ValueError('Incomplete approvals.')
            if self.state=='rejected' and self.stages.count('rejected')!=1:raise ValueError('Missing rejection.')
            if self.state=='needs_information' and self.stages.count('information_requested')!=1:raise ValueError('Missing information request.')
            if self.state in {'rejected','needs_information'}:
                marker='rejected' if self.state=='rejected' else 'information_requested'
                i=self.stages.index(marker)
                following='void' if self.state=='rejected' else 'pending'
                if any(s!='approved' for s in self.stages[:i]) or any(s!=following for s in self.stages[i+1:]):raise ValueError('Invalid historical stage order.')
        else:raise ValueError('Invalid claim state.')


def initial_state(claim_id,financial):
    return ClaimState(claim_id,financial,'pending_approval',1,1,1,('pending',)*len(financial.approvers))


def decide(current, *, actor_id, expected_version, revision, stage, decision, reason):
    try:
        identifier(actor_id);integer(expected_version);integer(revision);integer(stage,maximum=10)
        # Preserve canonical v1 text semantics:4096 UTF-8 bytes, only NUL denied.
        # Whitespace is checked for required reasons, never normalized in storage.
        if type(reason) is not str or len(reason.encode())>4096 or '\x00' in reason:raise ValueError
        if decision not in {'approve','reject','request_information'} or (decision!='approve' and not reason.strip()):raise ValueError
    except (ValueError,TypeError):raise ClaimFailure(400) from None
    if current.state!='pending_approval' or (expected_version,revision,stage)!=(current.version,current.revision,current.active_stage):raise ClaimFailure(409)
    if actor_id==current.financial.owner_id or actor_id!=current.financial.approvers[stage-1]:raise ClaimFailure(403)
    stages=list(current.stages)
    stages[stage-1]={'approve':'approved','reject':'rejected','request_information':'information_requested'}[decision]
    if decision=='approve' and stage<len(stages):state,active='pending_approval',stage+1
    elif decision=='approve':state,active='approved',None
    elif decision=='reject':
        state,active='rejected',None
        stages[stage:]=['void']*(len(stages)-stage)
    else:state,active='needs_information',None
    return replace(current,state=state,version=current.version+1,active_stage=active,stages=tuple(stages))


def provide_information(current, financial, *, actor_id, expected_version, revision):
    if type(financial) is not Financial:raise ClaimFailure(400)
    if actor_id!=current.financial.owner_id:raise ClaimFailure(403)
    if (current.state!='needs_information' or type(expected_version) is not int or type(revision) is not int
            or (expected_version,revision)!=(current.version,current.revision)):raise ClaimFailure(409)
    old=current.financial
    if (financial.tenant_id,financial.owner_id,financial.trip_id,financial.locked_policy,financial.approvers)!=(
            old.tenant_id,old.owner_id,old.trip_id,old.locked_policy,old.approvers):raise ClaimFailure(409)
    return replace(current,financial=financial,state='pending_approval',version=current.version+1,
                   revision=current.revision+1,active_stage=1,stages=('pending',)*len(financial.approvers))


@dataclass(frozen=True)
class ClaimPermit:
    """Immutable verified-source evidence, supplied ONLY by a trusted authority port.

    This is not a public DTO or a cryptographic verifier. FILE/ACT/FLOW providers
    must verify their original source proofs before constructing this value.
    Current ports default to deny. No raw JWT, private key or file content lives here.
    """
    evidence: bytes = field(repr=False)

    def checked(self,*,request_hash,operation_id,operation_key,candidate_claim_id,policy,operation_kind,trip_date,claim_ref,receipts,actor_expires_at,now):
        def pairs(rows):
            result={}
            for key,value in rows:
                if key in result:raise ValueError
                result[key]=value
            return result
        def bad(_):raise ValueError
        try:
            if type(self.evidence) is not bytes or not 1<=len(self.evidence)<=16384:raise ValueError
            data=json.loads(self.evidence,object_pairs_hook=pairs,parse_constant=bad)
            if type(data) is not dict or set(data)!={'request_hash','operation_id','operation_key','candidate_claim_id','expires_at','human_receipt_id','human_action_ref','eligibility','receipt_pins'}:raise ValueError
            identifier(candidate_claim_id);identifier(operation_key)
            if data['candidate_claim_id']!=candidate_claim_id or data['operation_key']!=operation_key:raise ValueError
            if data['request_hash']!=request_hash or data['operation_id']!=operation_id:raise ValueError
            identifier(data['human_receipt_id']);identifier(data['human_action_ref'])
            expiry=integer(data['expires_at'])
            if not now<expiry<=actor_expires_at:raise ValueError
            elig=data['eligibility']
            if type(elig) is not dict or set(elig)!={'allow','policy','operation_id','request_hash','operation_kind','trip_date','claim_ref','eligibility_revision','checked_at','expires_at','blocked_reason'}:raise ValueError
            if (elig['allow'] is not True or elig['blocked_reason'] is not None or elig['policy']!=policy
                or elig['operation_id']!=operation_id or elig['request_hash']!=request_hash
                or elig['operation_kind']!=operation_kind or elig['trip_date']!=trip_date or elig['claim_ref']!=claim_ref):raise ValueError
            if type(elig['policy']) is not dict or set(elig['policy'])!={'id','version','digest'}:raise ValueError
            identifier(elig['policy']['id']);integer(elig['policy']['version'])
            if type(elig['policy']['digest']) is not str or not re.fullmatch('[a-f0-9]{64}',elig['policy']['digest']):raise ValueError
            integer(elig['eligibility_revision']);integer(elig['checked_at']);integer(elig['expires_at'])
            if not elig['checked_at']<=now+5 or not elig['checked_at']<elig['expires_at']<=elig['checked_at']+60 or expiry>elig['expires_at']:raise ValueError
            if type(data['receipt_pins']) is not list or len(data['receipt_pins'])!=len(receipts) or len(receipts)>10:raise ValueError
            expected={r['id']:(r['version'],r['digest']) for r in receipts};seen=set()
            for pin in data['receipt_pins']:
                if type(pin) is not dict or set(pin)!={'id','version','digest','pin_operation_ref','attestation_digest','expires_at','operation_id','request_hash','candidate_claim_id'}:raise ValueError
                identifier(pin['id']);identifier(pin['pin_operation_ref']);integer(pin['version']);integer(pin['expires_at'])
                if pin['id'] in seen or expected.get(pin['id'])!=(pin['version'],pin['digest']):raise ValueError
                if type(pin['attestation_digest']) is not str or not re.fullmatch('[a-f0-9]{64}',pin['attestation_digest']):raise ValueError
                if pin['operation_id']!=operation_id or pin['request_hash']!=request_hash or pin['candidate_claim_id']!=candidate_claim_id or pin['expires_at']<expiry:raise ValueError
                seen.add(pin['id'])
            if seen!=set(expected):raise ValueError
            return data
        except (ValueError,TypeError,KeyError,OverflowError):raise ClaimFailure(503) from None
