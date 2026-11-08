"""Untrusted model proposals are editable input, never business authority."""
from uuid import uuid5,NAMESPACE_URL,UUID
class ProposalFailure(ValueError):
    def __init__(self,status=400):self.status=status;super().__init__('Invalid assisted input.')

def exact(value,fields):
    if type(value) is not dict or set(value)!=set(fields):raise ValueError

def identifier(value):
    if type(value) is not str or str(UUID(value))!=value or UUID(value).int==0:raise ValueError

def integer(value):
    if type(value) is not int or not 1<=value<=9223372036854775807:raise ValueError

PREPARE='prepare_reimbursement'
CLARIFY='clarify_reimbursement'

def selection(value):
    try:
        exact(value,{'kind','trip_id','claim_id','receipts'})
        identifier(value['trip_id'])
        if value['kind'] not in {'submission','information_update'}:raise ValueError
        if value['kind']=='submission' and value['claim_id'] is not None:raise ValueError
        if value['kind']=='information_update':identifier(value['claim_id'])
        if type(value['receipts']) is not list or len(value['receipts'])>20:raise ValueError
        seen=set()
        for r in value['receipts']:
            exact(r,{'id','version'});identifier(r['id']);integer(r['version'])
            if r['id'] in seen:raise ValueError
            seen.add(r['id'])
        return value
    except (ValueError,TypeError,KeyError):raise ProposalFailure(400) from None

def proposal(value):
    try:
        exact(value,{'name','input'})
        args=value['input']
        if value['name']==CLARIFY:
            # Fixed codes only; arbitrary model prose never enters storage/output.
            exact(args,{'reason'})
            if args['reason'] not in {'missing_amount','missing_receipt','ambiguous_request'}:raise ValueError
        elif value['name']==PREPARE:
            exact(args,{'currency','items'})
            if args['currency'] not in {'IDR','SGD','USD'} or type(args['items']) is not list or not 1<=len(args['items'])<=8:raise ValueError
            seen=set()
            for item in args['items']:
                exact(item,{'trip_item_id','amount_minor','receipt_ids'});identifier(item['trip_item_id']);integer(item['amount_minor'])
                if item['trip_item_id'] in seen or type(item['receipt_ids']) is not list or len(item['receipt_ids'])>20:raise ValueError
                seen.add(item['trip_item_id'])
                for ref in item['receipt_ids']:identifier(ref)
                if len(set(item['receipt_ids']))!=len(item['receipt_ids']):raise ValueError
        else:raise ValueError
        return value
    except (ValueError,TypeError,KeyError):raise ProposalFailure(400) from None

def stable(run_id,step):return str(uuid5(NAMESPACE_URL,'urn:enterprise-ai:assisted:v1:'+run_id+':'+step))

def content(run_id,value,selected,trip):
    value=proposal(value)
    if value['name']!=PREPARE:raise ProposalFailure(400)
    args=value['input'];known={x['id']:x for x in trip['items']};allowed={r['id'] for r in selected['receipts']}
    for item in args['items']:
        if item['trip_item_id'] not in known or known[item['trip_item_id']]['currency']!=args['currency'] or not set(item['receipt_ids'])<=allowed:raise ProposalFailure(400)
    used={ref for item in args['items'] for ref in item['receipt_ids']}
    return dict(currency=args['currency'],items=[dict(id=stable(run_id,x['trip_item_id']),**x) for x in args['items']],receipts=[r for r in selected['receipts'] if r['id'] in used])
