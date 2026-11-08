"""Strict v2 transport records. Schema v1 never gains effect permission."""
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED,PrivacyFailure
from enterprise_ai_platform.modules.privacy.domain.effects import CopyClass,ClockBasis,CopyRule,EffectPolicy


def copy_rules_from_json(rows):
    if type(rows) is not list or not 1<=len(rows)<=128:raise PrivacyFailure('invalid')
    result=[]
    for row in rows:
        if type(row) is not dict or set(row)!={'category','store','copy_class','min_retain_seconds','expire_after_seconds','clock_basis','regions','provider_retention_seconds','training_allowed'}:raise PrivacyFailure('invalid')
        if type(row['regions']) is not list or any(type(v) is not str for v in row['regions']):raise PrivacyFailure('invalid')
        try:rule=CopyRule(Category(row['category']),row['store'],CopyClass(row['copy_class']),row['min_retain_seconds'],row['expire_after_seconds'],ClockBasis(row['clock_basis']),tuple(row['regions']),row['provider_retention_seconds'],row['training_allowed'])
        except (ValueError,TypeError):raise PrivacyFailure('invalid') from None
        rule.validate();result.append(rule)
    if len({r.key() for r in result})!=len(result) or {(r.category,r.store) for r in result}!={(c,s) for c,stores in REQUIRED.items() for s in stores}:raise PrivacyFailure('invalid')
    return tuple(result)


def effect_policy_from_json(value):
    if type(value) is not dict or set(value)!={'schema_version','tenant_id','revision','approved_by','approved_at','expires_at','copy_rules'}:raise PrivacyFailure('invalid')
    policy=EffectPolicy(value['schema_version'],value['tenant_id'],value['revision'],value['approved_by'],value['approved_at'],value['expires_at'],copy_rules_from_json(value['copy_rules']))
    policy.validate(policy.approved_at);return policy
