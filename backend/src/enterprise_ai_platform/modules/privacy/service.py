"""One durable, version-bound lifecycle step; ambiguous effects stay held."""
import time
from enterprise_ai_platform.modules.privacy.domain import Category,Policy,Grant,Inventory,Asset,PrivacyFailure,identifier,digest


class PrivacyService:
    def __init__(self,repository,policies,authority,stores,journal,*,clock=time.time):
        self.repository,self.policies,self.authority,self.stores,self.journal=repository,policies,authority,stores,journal
        self.clock=clock

    async def grant(self,credential,tenant,subject,operation,category):
        value=await self.authority.current(credential,tenant,subject,operation,category)
        if type(value) is not Grant:raise PrivacyFailure('denied')
        value.validate(tenant,subject,operation,category,self.clock());return value

    async def policy(self,tenant):
        value=await self.policies.current(tenant)
        if type(value) is not Policy or value.tenant_id!=tenant:raise PrivacyFailure('policy_missing')
        value.validate(self.clock());return value

    async def save(self,body):
        return await self.repository.save(body['tenant_id'],body['id'],body['revision'],body)

    async def hold(self,body,reason):
        body.update(state='held',reason=reason);return await self.save(body)

    async def start(self,credential,tenant,subject,operation,category,operation_id):
        await self._start(credential,tenant,subject,operation,category,operation_id)
        return await self.status(credential,tenant,operation_id)

    async def _start(self,credential,tenant,subject,operation,category,operation_id):
        identifier(tenant);identifier(subject);identifier(operation_id)
        if type(category) is not Category or operation not in {'delete','export'}:raise PrivacyFailure('invalid')
        grant=await self.grant(credential,tenant,subject,operation,category)
        request=digest(dict(tenant=tenant,subject=subject,operation=operation,category=category.value,actor=grant.actor_id))
        existing=await self.repository.get(tenant,operation_id)
        if existing:
            if existing['request_digest']!=request:raise PrivacyFailure('conflict')
            return existing
        policy=await self.policy(tenant);rule=next(v for v in policy.rules if v.category is category)
        # Journal durability precedes acknowledgment and any scope hold side effect.
        await self.journal.intent(tenant,operation_id,'scope',dict(subject_id=subject,operation=operation,category=category.value,request_digest=request))
        if operation=='delete':
            current=await self.grant(credential,tenant,subject,operation,category)
            if current!=grant or (await self.policy(tenant)).fingerprint()!=policy.fingerprint():raise PrivacyFailure('denied')
            await self.authority.hold(current,operation_id,policy_digest=policy.fingerprint())
        await self.journal.result(tenant,operation_id,'scope','held' if operation=='delete' else 'recorded')
        inventory=await self.stores.inventory(grant,category)
        if type(inventory) is not Inventory:raise PrivacyFailure('held')
        inventory.validate(grant,rule)
        # Revalidate after inventory's awaits, before returning an accepted plan.
        if await self.grant(credential,tenant,subject,operation,category)!=grant:raise PrivacyFailure('denied')
        current=await self.policy(tenant)
        if current.fingerprint()!=policy.fingerprint():raise PrivacyFailure('conflict')
        now=self.clock();reason=''
        if any(a.region not in rule.regions for a in inventory.assets):reason='residency'
        if operation=='delete' and any(a.unresolved or now<max(a.retain_until,a.created_at+rule.retention_seconds) for a in inventory.assets):reason='retention_or_unresolved'
        body=dict(id=operation_id,tenant_id=tenant,subject_id=subject,actor_id=grant.actor_id,grant_revision=grant.revision,
            operation=operation,category=category.value,request_digest=request,policy_digest=policy.fingerprint(),
            state='held' if reason else 'queued',reason=reason,revision=1,
            steps=[dict(asset=a.record(),effect_key=digest(dict(operation_id=operation_id,asset=a.record())),state='pending') for a in sorted(inventory.assets,key=lambda a:(a.store,a.resource_id))])
        return await self.repository.create(tenant,operation_id,request,body)

    async def status(self,credential,tenant,operation_id):
        identifier(tenant);identifier(operation_id);body=await self.repository.get(tenant,operation_id)
        if body is None:raise PrivacyFailure('denied')
        grant=await self.grant(credential,tenant,body['subject_id'],body['operation'],Category(body['category']))
        if grant.actor_id!=body['actor_id']:raise PrivacyFailure('denied')
        return body

    async def advance(self,credential,tenant,operation_id):
        await self._advance(credential,tenant,operation_id)
        # Result persistence can await storage; reauthorize the final response.
        return await self.status(credential,tenant,operation_id)

    async def _advance(self,credential,tenant,operation_id):
        body=await self.status(credential,tenant,operation_id)
        if body['state'] in {'held','complete'}:return body
        try:
            policy=await self.policy(tenant)
            if policy.fingerprint()!=body['policy_digest']:return await self.hold(body,'policy_changed')
            grant=await self.grant(credential,tenant,body['subject_id'],body['operation'],Category(body['category']))
            if grant.actor_id!=body['actor_id'] or grant.revision!=body['grant_revision']:return await self.hold(body,'authority_changed')
        except PrivacyFailure:return await self.hold(body,'authority_or_policy_unavailable')
        step=next((s for s in body['steps'] if s['state']!='complete'),None)
        if step is None:
            body.update(state='complete',reason='');return await self.save(body)
        asset=Asset(**step['asset']);rule=next(v for v in policy.rules if v.category.value==body['category'])
        if asset.region not in rule.regions:return await self.hold(body,'residency')
        if body['operation']=='delete' and (asset.unresolved or self.clock()<max(asset.retain_until,asset.created_at+rule.retention_seconds)):
            return await self.hold(body,'retention_or_unresolved')
        if step['state']=='intent':
            outcome=await self.stores.reconcile(grant,asset,body['operation'],step['effect_key'],policy_digest=body['policy_digest'])
            if outcome=='sealed_not_applied':
                await self.journal.result(tenant,operation_id,step['effect_key'],'not_applied')
                return await self.hold(body,'effect_sealed_manual_review')
            # Another worker may still own the in-flight effect. Absence/unknown
            # is not permission to rearm, and must not clobber its plan revision.
            if outcome!='applied':return body
            await self.journal.result(tenant,operation_id,step['effect_key'],'applied')
            step['state']='complete';return await self.save(body)
        if await self.stores.current(grant,asset,policy_digest=body['policy_digest']) is not True:return await self.hold(body,'asset_changed')
        await self.journal.intent(tenant,operation_id,step['effect_key'],dict(operation=body['operation'],asset=asset.record(),policy_digest=body['policy_digest']))
        # Persist intent before effect, with CAS so concurrent workers cannot both apply.
        step['state']='intent';body=await self.save(body)
        step=next(s for s in body['steps'] if s['effect_key']==step['effect_key'])
        # Intent/save may await storage: authority, policy and exact object get a final gate.
        latest=await self.grant(credential,tenant,body['subject_id'],body['operation'],Category(body['category']))
        if latest!=grant or (await self.policy(tenant)).fingerprint()!=body['policy_digest'] or await self.stores.current(latest,asset,policy_digest=body['policy_digest']) is not True:
            return await self.hold(body,'pre_effect_changed')
        outcome=await self.stores.apply(latest,asset,body['operation'],step['effect_key'],policy_digest=body['policy_digest'])
        if outcome not in {'applied','not_applied','unknown','retained'}:outcome='unknown'
        # Keep a known effect even if access was revoked while the external call ran.
        await self.journal.result(tenant,operation_id,step['effect_key'],outcome)
        if outcome!='applied':return await self.hold(body,'effect_'+outcome)
        step['state']='complete';body=await self.save(body)
        try:
            after=await self.grant(credential,tenant,body['subject_id'],body['operation'],Category(body['category']))
            if after!=grant:return await self.hold(body,'post_effect_authority_changed')
        except PrivacyFailure:return await self.hold(body,'post_effect_authority_changed')
        return body
