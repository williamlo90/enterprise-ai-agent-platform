"""Strict admin commands; supplied identity and URL fields never grant authority."""
import re
from uuid import UUID

def exact(v,fields):
    if type(v) is not dict or set(v)!=set(fields):raise ValueError

def identifier(v):
    if type(v) is not str or str(UUID(v))!=v or UUID(v).int==0:raise ValueError

def integer(v,low=1,high=999999999999999999):
    if type(v) is not int or not low<=v<=high:raise ValueError

def text(v,limit,*,multiline=False):
    if type(v) is not str or not v.strip() or len(v)>limit or len(v.encode())>limit*4 or any((ord(c)<32 or ord(c)==127) and not (multiline and c in '\r\n\t') for c in v):raise ValueError

def reference(v):
    if type(v) is not str or not re.fullmatch('[a-z][a-z0-9_.-]{0,127}',v):raise ValueError

def config(v):
    exact(v,{'assistant_name','locales','help_contact','instruction_text','connector_ref','model_policy_ref'})
    for k,n in [('assistant_name',200),('help_contact',500),('instruction_text',16000)]:text(v[k],n,multiline=k=='instruction_text')
    if v['locales'] not in [['id'],['en'],['id','en'],['en','id']]:raise ValueError
    reference(v['connector_ref']);reference(v['model_policy_ref'])

def mutation(operation,v):
    shapes={'member_create':{'key','principal_id'},'member_active':{'id','expected_version','active','key'},'role':{'id','expected_version','key','operation','role','scope_kind','scope_ref'},'config_write':{'expected_version','key','config'},'provision':{'key','slug','name','timezone','initial_admin_id'}}
    exact(v,shapes[operation]);identifier(v['key'])
    for k in ('id','principal_id','initial_admin_id'):
        if k in v:identifier(v[k])
    if 'expected_version' in v:integer(v['expected_version'])
    if operation=='member_active' and type(v['active']) is not bool:raise ValueError
    if operation=='config_write':config(v['config'])
    if operation=='role':
        if v['operation'] not in {'grant','revoke'} or v['role'] not in {'employee','knowledge_manager','tenant_admin','process_owner','approver'}:raise ValueError
        if v['scope_kind'] not in {'tenant','knowledge_collection','process'}:raise ValueError
        if v['scope_kind']=='process':
            if v['scope_ref']!='reimbursement':raise ValueError
        else:identifier(v['scope_ref'])
    if operation=='provision':
        text(v['name'],200)
        if type(v['slug']) is not str or not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*',v['slug']) or len(v['slug'])>80:raise ValueError
        if v['timezone'] not in {'Asia/Jakarta','Asia/Singapore','UTC'}:raise ValueError
    return v

def page(after,limit,*,policy=False):
    integer(limit,1,20)
    if after is not None:
        if policy:
            if type(after) is not str:raise ValueError
            id,version=after.split(':');identifier(id)
            if not re.fullmatch('[1-9][0-9]{0,17}',version):raise ValueError
        else:identifier(after)
