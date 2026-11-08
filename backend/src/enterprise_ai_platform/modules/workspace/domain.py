"""Independent browser projections; service responses are never arbitrary JSON."""
import hashlib
import json
from uuid import UUID
from datetime import date,datetime

def exact(v,fields):
    if type(v) is not dict or set(v)!=set(fields):raise ValueError

def identifier(v):
    if type(v) is not str or str(UUID(v))!=v or UUID(v).int==0:raise ValueError

def integer(v,low=1,high=9223372036854775807):
    if type(v) is not int or not low<=v<=high:raise ValueError

def text(v,maximum):
    if type(v) is not str or len(v.encode())>maximum or any(ord(c)<32 and c not in '\r\n\t' for c in v):raise ValueError

def reason(v):
    # Exact accepted ACT/v1 semantics, including non-NUL controls and whitespace.
    if type(v) is not str or '\x00' in v or len(v.encode('utf-8'))>4096:raise ValueError

def query(view,claim_id,after,limit):
    if view not in {'mine','approvals'}:raise ValueError
    for v in (claim_id,after):
        if v is not None:identifier(v)
    integer(limit,1,20)
    if claim_id is not None and (after is not None or limit!=1):raise ValueError
    return dict(view=view,claim_id=claim_id,after=after,limit=limit)

def digest(values):return hashlib.sha256(b'reimbursement:workspace:v1:'+json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def summary(v,view,actor):
    exact(v,{'id','public_reference','owner_id','state','version','revision','active_stage','total_minor','currency','submitted_at','needs_action'})
    identifier(v['id']);identifier(v['owner_id']);text(v['public_reference'],64)
    for k in ('version','revision','total_minor'):integer(v[k])
    if v['currency'] not in {'IDR','SGD','USD'} or v['state'] not in {'pending_approval','needs_information','approved','rejected'}:raise ValueError
    if v['state']=='pending_approval':integer(v['active_stage'],1,10)
    elif v['active_stage'] is not None:raise ValueError
    if not isinstance(v['submitted_at'],str) or datetime.fromisoformat(v['submitted_at']).tzinfo is None:raise ValueError
    if type(v['needs_action']) is not bool or v['needs_action']!=(view=='approvals' or v['state']=='needs_information'):raise ValueError
    if view=='mine' and v['owner_id']!=actor:raise ValueError
    if view=='approvals' and (v['owner_id']==actor or v['state']!='pending_approval'):raise ValueError

def detail(v,view,actor,tenant,claim_id):
    exact(v,{'tenant_id','id','owner_id','trip_id','trip_date','policy','state','version','revision','active_stage','reviewer_id','public_reference','submitted_at','previous_claim_id','financial','items','receipts','stages','trip','rules','decisions'})
    if v['tenant_id']!=tenant or v['id']!=claim_id:raise ValueError
    for k in ('id','owner_id','trip_id'):identifier(v[k])
    for k in ('reviewer_id','previous_claim_id'):
        if v[k] is not None:identifier(v[k])
    financial=v['financial'];exact(financial,{'currency','total_minor'})
    summary({**{k:v[k] for k in ('id','public_reference','owner_id','state','version','revision','active_stage','submitted_at')},**financial,'needs_action':view=='approvals' or v['state']=='needs_information'},view,actor)
    if view=='approvals' and v['reviewer_id']!=actor:raise ValueError
    if type(v['items']) is not list or not 1<=len(v['items'])<=100 or type(v['receipts']) is not list or len(v['receipts'])>100:raise ValueError
    used=set();lines=set();amount=0
    for item in v['items']:
        exact(item,{'id','trip_item_id','amount_minor','receipt_ids'});identifier(item['id']);identifier(item['trip_item_id']);integer(item['amount_minor'])
        if item['id'] in lines or type(item['receipt_ids']) is not list or len(item['receipt_ids'])>100:raise ValueError
        lines.add(item['id']);amount+=item['amount_minor']
        for ref in item['receipt_ids']:identifier(ref);used.add(ref)
    if amount!=financial['total_minor']:raise ValueError
    refs=set()
    for ref in v['receipts']:
        exact(ref,{'id','version','digest'});identifier(ref['id']);integer(ref['version'])
        if ref['id'] in refs or type(ref['digest']) is not str or len(ref['digest'])!=64 or any(c not in '0123456789abcdef' for c in ref['digest']):raise ValueError
        refs.add(ref['id'])
    if refs!=used:raise ValueError
    trip=v['trip'];exact(trip,{'id','start_date','end_date','employee_ref','state'})
    if trip['id']!=v['trip_id'] or trip['state']!='completed':raise ValueError
    text(trip['employee_ref'],128)
    for key in ('start_date','end_date'):
        if date.fromisoformat(trip[key]).isoformat()!=trip[key]:raise ValueError
    if trip['end_date']!=v['trip_date'] or trip['start_date']>trip['end_date']:raise ValueError
    exact(v['policy'],{'id','version','digest'});identifier(v['policy']['id']);integer(v['policy']['version'])
    if type(v['policy']['digest']) is not str or len(v['policy']['digest'])!=64 or any(c not in '0123456789abcdef' for c in v['policy']['digest']):raise ValueError
    exact(v['rules'],{'timezone','rules'});text(v['rules']['timezone'],100)
    rules=v['rules']['rules'];exact(rules,{'currency','submission_deadline_days','max_total_minor','categories','approval_route'})
    if rules['currency']!=financial['currency']:raise ValueError
    integer(rules['submission_deadline_days'],0,3650)
    if rules['max_total_minor'] is not None:integer(rules['max_total_minor'])
    if type(rules['categories']) is not list or not 1<=len(rules['categories'])<=100:raise ValueError
    for cat in rules['categories']:
        exact(cat,{'code','receipt_required','max_amount_minor'});text(cat['code'],64)
        if type(cat['receipt_required']) is not bool:raise ValueError
        if cat['max_amount_minor'] is not None:integer(cat['max_amount_minor'])
    if type(v['stages']) is not list or not 1<=len(v['stages'])<=10 or type(rules['approval_route']) is not list or len(rules['approval_route'])!=len(v['stages']):raise ValueError
    for n,(s,route) in enumerate(zip(v['stages'],rules['approval_route']),1):
        exact(s,{'stage','approver_id','scope_ref','state'});exact(route,{'stage','approver_id','scope_ref'});identifier(s['approver_id'])
        if type(s['stage']) is not int or s['stage']!=n or route!={k:s[k] for k in ('stage','approver_id','scope_ref')} or s['scope_ref']!='reimbursement' or s['state'] not in {'pending','approved','rejected','information_requested','void'}:raise ValueError
    if type(v['decisions']) is not list or len(v['decisions'])>10:raise ValueError
    for d in v['decisions']:
        exact(d,{'stage','actor_id','decision','reason','created_at'});integer(d['stage'],1,10);identifier(d['actor_id']);reason(d['reason'])
        if d['decision'] not in {'approve','reject','request_information'} or (d['decision']!='approve' and not d['reason'].strip()):raise ValueError
        if datetime.fromisoformat(d['created_at']).tzinfo is None:raise ValueError

def projection(value,values,actor,tenant):
    if values['claim_id'] is not None:
        exact(value,{'claim'});detail(value['claim'],values['view'],actor,tenant,values['claim_id'])
    else:
        exact(value,{'items','next_after'})
        if type(value['items']) is not list or len(value['items'])>values['limit']:raise ValueError
        ids=[]
        for item in value['items']:summary(item,values['view'],actor);ids.append(item['id'])
        if ids!=sorted(set(ids)) or (values['after'] is not None and any(i<=values['after'] for i in ids)):raise ValueError
        if value['next_after'] is not None and (len(ids)!=values['limit'] or value['next_after']!=ids[-1]):raise ValueError
    return value
