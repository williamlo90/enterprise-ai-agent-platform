"""Original-session policy publication; privileged producer owns the commit."""
import math
import re
import time
from enterprise_ai_platform.modules.privacy.domain import Rule,Category,Policy,PrivacyFailure,identifier,instant
from enterprise_ai_platform.modules.identity.service import opaque,digest
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.modules.privacy.ports import copy_rules_from_json,effect_policy_from_json


def rules_from_json(rows):
    if type(rows) is not list or len(rows)!=len(Category):raise PrivacyFailure('invalid')
    rules=[]
    for r in rows:
        if type(r) is not dict or set(r)!={'category','retention_seconds','regions','stores','provider_retention_seconds','training_allowed'}:raise PrivacyFailure('invalid')
        if type(r['regions']) is not list or type(r['stores']) is not list or any(type(v) is not str for v in r['regions']+r['stores']):raise PrivacyFailure('invalid')
        if len(set(r['stores']))!=len(r['stores']):raise PrivacyFailure('invalid')
        try:category=Category(r['category'])
        except (ValueError,TypeError):raise PrivacyFailure('invalid') from None
        rule=Rule(category,r['retention_seconds'],tuple(r['regions']),frozenset(r['stores']),r['provider_retention_seconds'],r['training_allowed'])
        rule.validate();rules.append(rule)
    if {r.category for r in rules}!=set(Category):raise PrivacyFailure('invalid')
    return tuple(rules)


def policy_from_json(value):
    if type(value) is not dict or set(value)!={'tenant_id','revision','approved_by','approved_at','expires_at','rules'}:raise PrivacyFailure('invalid')
    policy=Policy(value['tenant_id'],value['revision'],value['approved_by'],value['approved_at'],value['expires_at'],rules_from_json(value['rules']))
    policy.validate(policy.approved_at);return policy


class PrivacyRegistryService:
    def __init__(self,repository,origin,*,clock=time.time):self.repository,self.origin,self.clock=repository,origin,clock

    async def fence_call(self,session,conversation_id,operation,values,headers=None):
        if not opaque(session):raise AccessFailure(401)
        if operation not in {'fence','fence_status','release','export','delete'}:raise AccessFailure(400)
        try:identifier(conversation_id)
        except PrivacyFailure:raise AccessFailure(400) from None
        payload=dict(values)
        if operation in {'fence','release','export','delete'}:
            headers=headers or {}
            if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise AccessFailure(403)
            try:
                if set(payload)!=({'key','version','content_digest','policy_hash'} if operation in {'fence','export'} else {'key','fence_key'}):raise PrivacyFailure('invalid')
                identifier(payload['key'])
                if payload['key']=='00000000-0000-0000-0000-000000000000':raise PrivacyFailure('invalid')
                if operation in {'fence','export'}:
                    if type(payload['version']) is not int or not 1<=payload['version']<10**18:raise PrivacyFailure('invalid')
                    if any(type(payload[k]) is not str or not re.fullmatch('[a-f0-9]{64}',payload[k]) for k in ('content_digest','policy_hash')):raise PrivacyFailure('invalid')
                else:
                    identifier(payload['fence_key'])
                    if payload['fence_key']=='00000000-0000-0000-0000-000000000000':raise PrivacyFailure('invalid')
            except PrivacyFailure:raise AccessFailure(400) from None
            payload['csrf_hash']=digest('csrf',headers['x-csrf-token'])
        elif payload:raise AccessFailure(400)
        payload['id']=conversation_id
        result=await self.repository.call(digest('session',session),operation,payload)
        deadline=result.pop('authority_expires_at',None)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise AccessFailure(401)
        return dict(result,_privacy_deadline=int(deadline))

    async def effect_call(self,session,operation,values,headers=None):
        if not opaque(session):raise AccessFailure(401)
        if operation not in {'current','publish'}:raise AccessFailure(400)
        payload=dict(values)
        if operation=='publish':
            headers=headers or {}
            if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise AccessFailure(403)
            try:
                if set(payload)!={'key','schema_version','revision','expires_at','copy_rules'} or type(payload['schema_version']) is not int or payload['schema_version']!=2:raise PrivacyFailure('invalid')
                identifier(payload['key'])
                if payload['key']=='00000000-0000-0000-0000-000000000000' or type(payload['revision']) is not int or not 1<=payload['revision']<10**18:raise PrivacyFailure('invalid')
                if not instant(payload['expires_at']) or not self.clock()<payload['expires_at']<=253402300799:raise PrivacyFailure('invalid')
                copy_rules_from_json(payload['copy_rules'])
            except PrivacyFailure:raise AccessFailure(400) from None
            payload['csrf_hash']=digest('csrf',headers['x-csrf-token'])
        elif payload:raise AccessFailure(400)
        result=await self.repository.call(digest('session',session),'effect_'+operation,payload)
        deadline=result.pop('authority_expires_at',None)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise AccessFailure(401)
        try:
            policy=effect_policy_from_json(result['policy']) if result.get('policy') is not None else None
            if operation=='publish' and (policy is None or policy.revision!=payload['revision'] or policy.expires_at!=payload['expires_at'] or policy.rules!=copy_rules_from_json(payload['copy_rules'])):raise PrivacyFailure('invalid')
        except (PrivacyFailure,KeyError,TypeError):raise AccessFailure(503) from None
        return dict(result,effective=bool(policy and self.clock()<policy.expires_at),_privacy_deadline=int(deadline))

    async def inventory(self,session,conversation_id):
        if not opaque(session):raise AccessFailure(401)
        try:identifier(conversation_id)
        except PrivacyFailure:raise AccessFailure(400) from None
        result=await self.repository.call(digest('session',session),'inventory',dict(id=conversation_id))
        deadline=result.pop('authority_expires_at',None)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise AccessFailure(401)
        return dict(result,_privacy_deadline=int(deadline))

    async def call(self,session,operation,values,headers=None):
        if not opaque(session):raise AccessFailure(401)
        if operation not in {'current','publish'}:raise AccessFailure(400)
        payload=dict(values)
        if operation=='publish':
            headers=headers or {}
            if headers.get('origin')!=self.origin or headers.get('content-type')!='application/json' or not opaque(headers.get('x-csrf-token')):raise AccessFailure(403)
            try:
                if set(payload)!={'key','revision','expires_at','rules'}:raise PrivacyFailure('invalid')
                identifier(payload['key'])
                if payload['key']=='00000000-0000-0000-0000-000000000000' or type(payload['revision']) is not int or not 1<=payload['revision']<=999999999999999999:raise PrivacyFailure('invalid')
                if not instant(payload['expires_at']) or not self.clock()<payload['expires_at']<=253402300799:raise PrivacyFailure('invalid')
                rules_from_json(payload['rules'])
            except PrivacyFailure:raise AccessFailure(400) from None
            payload['csrf_hash']=digest('csrf',headers['x-csrf-token'])
        elif payload:raise AccessFailure(400)
        result=await self.repository.call(digest('session',session),operation,payload)
        deadline=result.pop('authority_expires_at',None)
        if type(deadline) not in (int,float) or not math.isfinite(deadline) or self.clock()>=deadline:raise AccessFailure(401)
        try:
            policy=policy_from_json(result['policy']) if result.get('policy') is not None else None
            if operation=='publish' and (policy is None or policy.revision!=payload['revision'] or policy.expires_at!=payload['expires_at'] or policy.rules!=rules_from_json(payload['rules'])):raise PrivacyFailure('invalid')
        except (PrivacyFailure,KeyError,TypeError):raise AccessFailure(503) from None
        return dict(result,effective=bool(policy and self.clock()<policy.expires_at),_privacy_deadline=int(deadline))
