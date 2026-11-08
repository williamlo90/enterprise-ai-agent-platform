"""Policy arithmetic/closed gates only; no business approval or storage effects."""
from dataclasses import replace
from uuid import uuid4
import pytest
from enterprise_ai_platform.modules.privacy.domain import Category,REQUIRED,PrivacyFailure
from enterprise_ai_platform.modules.privacy.effect_policy import CopyClass,ClockBasis,CopyRule,EffectPolicy,CopyFacts,Mode,Hold,decide,plan_export


def uid():return str(uuid4())


def policy():
    classes={'db':CopyClass.CONTENT,'file':CopyClass.CONTENT,'index':CopyClass.INDEX,'cache':CopyClass.CACHE,'checkpoint':CopyClass.CHECKPOINT,'ledger':CopyClass.CANONICAL,'pins':CopyClass.CANONICAL,'backup':CopyClass.BACKUP,'provider':CopyClass.PROVIDER}
    derived={'index','cache','checkpoint','backup','provider'}
    rows=tuple(CopyRule(c,s,classes[s],0,100,ClockBasis.COPY_CREATED if s in derived else ClockBasis.CREATED,('ap-southeast-1',),0 if s=='provider' else 'not_applicable',False) for c,stores in REQUIRED.items() for s in sorted(stores))
    return EffectPolicy(2,uid(),1,uid(),0,2000,rows)


def asset(p,**kw):
    values=dict(tenant_id=p.tenant_id,resource_id=uid(),version='7',content_digest='a'*64,category=Category.CONVERSATIONS,store='db',copy_class=CopyClass.CONTENT,region='ap-southeast-1',created_at=900,copy_created_at=None)
    values.update(kw);return CopyFacts(**values)


def changed(p,category=Category.CONVERSATIONS,store='db',**kw):
    return replace(p,rules=tuple(replace(r,**kw) if (r.category,r.store)==(category,store) else r for r in p.rules))


@pytest.mark.parametrize('field,value',[
    ('min_retain_seconds',True),('min_retain_seconds',None),('min_retain_seconds',-1),('min_retain_seconds',101),
    ('expire_after_seconds',0),('expire_after_seconds',True),('expire_after_seconds',float('inf')),
    ('clock_basis',ClockBasis.COPY_CREATED),('regions',()),('regions',(['invalid'],)),('training_allowed',True),
    ('provider_retention_seconds',0),
])
def test_v2_rules_require_explicit_consistent_class_clock_and_min_max(field,value):
    p=changed(policy(),**{field:value})
    with pytest.raises(PrivacyFailure):p.validate(1000)


def test_missing_pair_duplicate_rule_legacy_schema_and_unconfigured_copy_class_are_closed():
    p=policy()
    for value in [replace(p,rules=p.rules[:-1]),replace(p,rules=p.rules+(p.rules[0],)),replace(p,schema_version=1)]:
        with pytest.raises(PrivacyFailure):value.validate(1000)
    assert decide(None,asset(p),Mode.REQUEST_DELETE,1000).reason=='effect_policy_required'
    assert decide(p,asset(p,copy_class=CopyClass.EXPORT),Mode.REQUEST_DELETE,1000).reason=='copy_class_unconfigured'


def test_request_delete_minimum_and_scheduled_expiry_are_different_explicit_modes():
    p=changed(policy(),min_retain_seconds=20);a=asset(p)
    assert decide(p,a,Mode.REQUEST_DELETE,919).status=='retained'
    assert decide(p,a,Mode.REQUEST_DELETE,920).status=='ready'
    assert decide(p,a,Mode.SCHEDULED_EXPIRY,920).status=='not_due'
    assert decide(p,a,Mode.SCHEDULED_EXPIRY,1000).status=='ready'
    assert decide(p,a,'request_delete',1000).status=='held'


def test_derived_copy_uses_own_clock_and_missing_clock_never_falls_back_to_old_source():
    p=changed(policy(),store='cache',min_retain_seconds=20);a=asset(p,store='cache',copy_class=CopyClass.CACHE,created_at=1,copy_created_at=990)
    result=decide(p,a,Mode.REQUEST_DELETE,1000)
    assert result.status=='retained' and result.minimum_at==1010 and result.expires_at==1090
    assert decide(p,replace(a,copy_created_at=None),Mode.REQUEST_DELETE,1000).reason=='copy_clock_unverified'
    assert decide(p,replace(a,copy_created_at=1001),Mode.REQUEST_DELETE,1000).status=='held'


def test_expired_ongoing_hold_requires_explicit_release_and_reports_overdue():
    p=policy();a=asset(p);hold=Hold(p.tenant_id,a.resource_id,uid(),'receipt_pin',10,80,None,None)
    result=decide(p,a,Mode.SCHEDULED_EXPIRY,1000,(hold,))
    assert result.status=='overdue_held' and result.reason=='hold_review_or_release_required'
    assert decide(p,a,Mode.SCHEDULED_EXPIRY,1000,(replace(hold,released_at=900),)).status=='ready'
    reviewed=replace(hold,expires_at=None,review_at=1050)
    assert decide(p,a,Mode.SCHEDULED_EXPIRY,1000,(reviewed,)).status=='overdue_held'
    assert decide(p,a,Mode.SCHEDULED_EXPIRY,1000,(replace(reviewed,review_at=None),)).status=='held'


def test_foreign_region_subject_hold_and_nonfinite_clock_never_allow_effect():
    p=policy();a=asset(p)
    for modified in [replace(a,tenant_id=uid()),replace(a,region='unverified'),replace(a,created_at=float('nan')),replace(a,content_digest='forged')]:
        assert decide(p,modified,Mode.REQUEST_DELETE,1000).status=='held'
    wrong=Hold(p.tenant_id,uid(),uid(),'owner_hold',0,1100,None,None)
    assert decide(p,a,Mode.REQUEST_DELETE,1000,(wrong,)).status=='held'


def test_export_gets_own_approved_max_not_canonical_financial_minimum_or_fresh_read_ttl():
    p=changed(policy(),copy_class=CopyClass.CANONICAL,min_retain_seconds=1000,expire_after_seconds=1200)
    extra=CopyRule(Category.CONVERSATIONS,'db',CopyClass.EXPORT,'not_applicable',60,ClockBasis.COPY_CREATED,('ap-southeast-1',),'not_applicable',False)
    p=replace(p,rules=p.rules+(extra,));p.validate(1000)
    canonical=asset(p,copy_class=CopyClass.CANONICAL,created_at=900)
    assert decide(p,canonical,Mode.REQUEST_DELETE,1000).status=='retained'
    artifact=asset(p,copy_class=CopyClass.EXPORT,copy_created_at=1000)
    first=plan_export(p,artifact,1000);later=plan_export(p,artifact,1030)
    assert first.status=='ready' and first.storage_expires_at==1060 and first.minimum_at==1000
    assert later.storage_expires_at==first.storage_expires_at and later.download_expires_at==first.download_expires_at
    assert plan_export(p,artifact,1060).status=='held'
    assert plan_export(p,canonical,1000).status=='held'


def test_provider_declared_maximum_zero_does_not_turn_into_general_copy_retention():
    p=policy();a=asset(p,store='provider',copy_class=CopyClass.PROVIDER,copy_created_at=1000)
    result=decide(p,a,Mode.SCHEDULED_EXPIRY,1000)
    assert result.status=='ready' and result.expires_at==1000
    with pytest.raises(PrivacyFailure):changed(p,store='provider',min_retain_seconds=1).validate(1000)


def test_policy_hash_covers_clock_class_ttl_and_schema_but_not_rule_order():
    p=policy();assert p.fingerprint()==replace(p,rules=tuple(reversed(p.rules))).fingerprint()
    for modified in [changed(p,expire_after_seconds=99),changed(p,min_retain_seconds=1),replace(p,revision=2)]:assert p.fingerprint()!=modified.fingerprint()
