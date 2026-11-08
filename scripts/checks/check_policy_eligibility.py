"""POL-01 real private eligibility/crypto/DB proof; controlled human receipts only."""
import argparse
import asyncio
import copy
import hashlib
import json
import logging
import secrets
import sys
import time
import traceback
from dataclasses import dataclass,replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
import httpx
from psycopg import sql
from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from check_policy_draft import Harness as KernelHarness,DRAFTS,manifest
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,Settings,load_settings
from enterprise_ai_platform.delegation_check import SigningFixture,Receipts,ISSUER,AUDIENCE,CALLER
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.delegation import PostgresDelegationRepository
from enterprise_ai_platform.infrastructure.database.policies import PostgresPolicyRepository
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.delegation.domain import HumanReceipt,request_hash,canonical_payload
from enterprise_ai_platform.modules.delegation.service import DelegationService
from enterprise_ai_platform.modules.identity.service import digest
from enterprise_ai_platform.modules.policies.domain import PolicyFailure
from enterprise_ai_platform.modules.policies.service import PolicyService
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'reference_service/src'))
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.integrations.platform.policies import PolicyEligibilityClient


@dataclass(frozen=True)
class ClaimBinding:
    tenant_id:str
    claim_id:str
    owner_id:str
    reviewer_id:str
    policy:dict
    trip_date:str
    revision:int
    stage:int
    active:bool=True
    claim_version:int=1
    draft_id:str|None=None
    draft_version:int=1

    async def verify(self,actor,payload,policy,trip_date,claim_ref):
        common=(self.active and actor['tenant_id']==self.tenant_id and claim_ref==self.claim_id==payload['object']['id']
            and payload['object']['version']==self.claim_version and policy==self.policy and trip_date==self.trip_date
            and payload['data']['revision']==self.revision)
        if payload['action']=='claim.decide':return common and actor['actor_id']==self.reviewer_id and payload['data']['stage']==self.stage
        if payload['action']=='claim.information':return common and actor['actor_id']==self.owner_id and payload['data']['draft']==dict(id=self.draft_id,version=self.draft_version)
        return False


class Harness(KernelHarness):
    async def authority(self,person,payload,*,session=None,lifetime=60):
        token,csrf=await self.session(person['tenant'],person['person']) if session is None else session
        person.update(token=token,csrf=csrf)
        sid=await self.scalar('SELECT id FROM {s}.sessions WHERE token_hash=%s',(digest('session',token),))
        revision=await self.scalar('SELECT access_revision FROM {s}.memberships WHERE id=%s',(person['member'],))
        now=int(time.time())
        claims=dict(iss=ISSUER,aud=AUDIENCE,caller=CALLER,sub=str(person['person']),tenant_id=str(person['tenant']),membership_id=str(person['member']),
            session_id=str(sid),access_revision=revision,action=payload['action'],object_kind=payload['object']['kind'],object_id=payload['object']['id'],
            object_version=payload['object']['version'],operation_id=payload['operation_id'],request_hash=request_hash(payload),jti=str(uuid4()),
            iat=now,nbf=now,exp=now+lifetime,human_receipt_id=str(uuid4()))
        receipt=HumanReceipt(claims['human_receipt_id'],claims['sub'],claims['session_id'],claims['tenant_id'],claims['operation_id'],claims['action'],
                             claims['object_kind'],claims['object_id'],claims['object_version'],claims['request_hash'],now)
        self.receipts.values[receipt.id]=receipt
        return self.signing.sign(claims),claims

    def submission(self,person):
        # Deliberately noncanonical input order: production service must normalize
        # items, receipt IDs and receipts before both verifications and SQL hash.
        receipts=[dict(id=str(uuid4()),version=1,digest='a'*64),dict(id=str(uuid4()),version=2,digest='b'*64)]
        receipts.sort(key=lambda item:item['id'],reverse=True)
        items=[dict(id=str(uuid4()),trip_item_id=str(uuid4()),amount_minor=100,receipt_ids=[r['id']]) for r in receipts]
        items.sort(key=lambda item:item['id'],reverse=True)
        return dict(schema_version=1,action='claim.submit',tenant_id=str(person['tenant']),actor_id=str(person['person']),operation_id=str(uuid4()),
            object=dict(kind='draft',id=str(uuid4()),version=1),data=dict(trip_id=str(uuid4()),currency='SGD',items=items,total_minor=200,
            receipts=receipts,policy=self.policy_fixture['policy'],approval_route=self.policy_fixture['content']['rules']['approval_route'],destination=AUDIENCE))

    def request(self,payload,kind='submission'):
        return dict(payload=payload,policy=self.policy_fixture['policy'],operation_kind=kind,trip_date='2026-05-01',claim_ref=None if kind=='submission' else payload['object']['id'])

    async def private(self,token,request,*,credential=None):
        app=create_internal_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=None,policies=self.policies,delegation=self.delegation))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://internal-fixture.example.test') as client:
            return await client.post('/internal/v1/policy-packages/'+request['policy']['id']+'/eligibility',json=request,
                headers={'Authorization':'Bearer '+(credential or self.credential),'X-EAI-Delegation':token,'Content-Type':'application/json'})

    async def verified(self,token,payload,credential=None):
        return await self.delegation.verify_private(credential or self.credential,token,payload)

    async def direct_eligibility(self,verified,request,credential=None):
        request={**request,'payload':json.loads(canonical_payload(request['payload']))}
        hashed=hashlib.sha256(('eai:internal-caller:v1:'+(credential or self.credential)).encode()).hexdigest()
        return await self.policy_repo.eligibility(dict(verified.actor),request,hashed,verified.reference.wire())

    async def wait_for_owned_runtime_lock(self,task):
        for _ in range(40):
            if await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock')",('eai_runtime_'+self.schema.rsplit('_',1)[1],)):return
            if task.done():break
            await asyncio.sleep(.02)
        raise CheckFailure('owned-eligibility-runtime-did-not-block')

    async def waiting_eligibility(self,verified,request):
        values={**request,'payload':json.loads(canonical_payload(request['payload']))}
        auth=dict(actor=dict(verified.actor),credential_hash=self.credential_hash,verification_ref=verified.reference.wire())
        async with self.database.transaction() as connection:
            # Owned fault-injection timing only: a 3.1s wait is below the existing
            # 5s statement timeout; extend the row-lock timeout for this one probe.
            await connection.execute("SET LOCAL lock_timeout='4500ms'")
            return (await (await connection.execute(sql.SQL('SELECT {}.policy_eligibility_call(%s,%s)').format(sql.Identifier(self.schema)),(Jsonb(auth),Jsonb(values)))) .fetchone())[0]

    async def policy_eligibility(self):
        try:
            await self._policy_eligibility()
        except Exception as error:
            # Preserve only frame locations and source-authored check names.
            # Exception text, token payloads and local variables remain private.
            write_json(Path('local/pol01/eligibility-diagnostic.json'),dict(
                error_type=type(error).__name__,last_check=self.checks[-1] if self.checks else None,
                frames=[dict(file=Path(f.filename).name,line=f.lineno,function=f.name)
                        for f in traceback.extract_tb(error.__traceback__)]))
            raise

    async def _policy_eligibility(self):
        await self.policy_kernel()
        self.signing=SigningFixture();self.receipts=Receipts();self.credential=secrets.token_urlsafe(32)
        self.credential_hash=hashlib.sha256(('eai:internal-caller:v1:'+self.credential).encode()).hexdigest()
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'policy-fixture',%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",
                           (uuid4(),self.credential_hash,AUDIENCE,CALLER,Jsonb(['claim.submit','claim.information','claim.decide'])))
        await self.execute("INSERT INTO {s}.delegation_keys VALUES(%s,'current',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(ISSUER,Jsonb(self.signing.jwk)))
        self.delegation=DelegationService(PostgresDelegationRepository(self.database),RSADelegationVerifier(),ISSUER,receipts=self.receipts)
        self.policy_repo=PostgresPolicyRepository(self.database)
        self.policies=PolicyService(self.policy_repo,'https://unused-browser.example.test',self.delegation)
        # A member without document reader ACL can obtain transaction metadata,
        # while knowledge content remains inaccessible.
        tenant=self.people[0]['tenant'];person_id=uuid4()
        member=await self.member(tenant,person_id,grants=(RoleGrant('employee','tenant',str(tenant)),))
        person=dict(tenant=tenant,person=person_id,member=member)
        payload=self.submission(person);request=self.request(payload);token,claims=await self.authority(person,payload)
        response=await self.private(token,request);require(response.status_code==200,'eligibility-http-'+str(response.status_code))
        result=response.json()
        require(result['allow'] is True and result['request_hash']==request_hash(payload),'real-eligibility-allow')
        require(set(result)=={'allow','policy','operation_id','request_hash','operation_kind','trip_date','claim_ref','eligibility_revision','checked_at','expires_at','blocked_reason'},'metadata-whitelist')
        require(response.headers.get('cache-control')=='no-store','private-no-store')
        require(all(value not in response.text for value in (token,self.credential,self.policy_fixture['source']['document_id'])),'private-response-no-secrets-or-docrefs')
        internal=create_internal_app(Settings(env='test',host='127.0.0.1',port=8000),services=ApplicationServices(health=None,policies=self.policies,delegation=self.delegation))
        client=PlatformClient(PlatformTarget('https://internal-fixture.example.test',self.credential),transport=httpx.ASGITransport(app=internal))
        try:
            service_result=await PolicyEligibilityClient(client).eligibility(token,payload,**{k:v for k,v in request.items() if k!='payload'})
            require(service_result.allow is True and service_result.request_hash==request_hash(payload),'reference-client-real-api-eligibility')
        finally:await client.close()
        require(await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE document_id=%s AND principal_id=%s',(self.policy_fixture['source']['document_id'],person_id))==0,'no-reader-acl-granted')
        self.people.append(person)
        require((await self.source_http(self.policy_fixture['source'],len(self.people)-1)).status_code==404,'metadata-does-not-grant-source-content')
        self.passed('actual_reference_client_private_http_crypto_receipt_reverification_and_restricted_db_allow_unsorted_financial_payload_with_metadata_only_and_no_reader_grant')

        # Both tokens bind same actor/operation/hash but distinct original sessions.
        first=await self.verified(token,payload)
        other_token,other_claims=await self.authority(person,payload)
        other=await self.verified(other_token,payload)
        require(first.reference.jti!=other.reference.jti and first.actor['request_hash']==other.actor['request_hash'],'distinct-exact-private-ref')
        await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE id=%s',(claims['session_id'],))
        try:await self.direct_eligibility(first,request)
        except PolicyFailure as error:require(error.status==401,'original-session-ref-status')
        else:raise CheckFailure('revoked-original-ref-aliased-active-session')
        require((await self.direct_eligibility(other,request))['allow'] is True,'other-original-session-still-active')
        self.passed('revoked_original_session_A_cannot_alias_active_session_B_with_same_actor_operation_and_hash')

        # Credential overlap retains independent exact contexts for one token/JTI.
        rotated=secrets.token_urlsafe(32);rotated_hash=hashlib.sha256(('eai:internal-caller:v1:'+rotated).encode()).hexdigest()
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) SELECT %s,caller_ref,%s,audience,delegation_caller,allowed_actions,expires_at FROM {s}.internal_caller_credentials WHERE credential_hash=%s",
                           (uuid4(),rotated_hash,self.credential_hash))
        overlap=await self.verified(other_token,payload,rotated)
        require(overlap.reference.jti==other.reference.jti and overlap.reference.credential_hash!=other.reference.credential_hash,'credential-overlap-separate-reference')
        await self.execute('UPDATE {s}.internal_caller_credentials SET active=false WHERE credential_hash=%s',(self.credential_hash,))
        try:await self.direct_eligibility(other,request)
        except PolicyFailure as error:require(error.status==401,'revoked-recipient-ref-status')
        else:raise CheckFailure('revoked-recipient-context-aliased-rotation')
        require((await self.direct_eligibility(overlap,request,rotated))['allow'] is True,'rotated-recipient-still-valid')
        await self.execute('UPDATE {s}.internal_caller_credentials SET active=true WHERE credential_hash=%s',(self.credential_hash,))
        self.passed('same_token_jti_recipient_rotation_keeps_exact_contexts_and_revoked_credential_cannot_alias_active_credential')

        await self.execute("UPDATE {s}.delegation_keys SET active=false WHERE issuer=%s AND kid='current'",(ISSUER,))
        try:await self.direct_eligibility(overlap,request,rotated)
        except PolicyFailure as error:require(error.status==401,'revoked-key-ref-status')
        else:raise CheckFailure('revoked-signing-key-context-accepted')
        await self.execute("UPDATE {s}.delegation_keys SET active=true WHERE issuer=%s AND kid='current'",(ISSUER,))
        self.passed('signing_key_revocation_after_private_verification_is_rechecked_by_eligibility_rpc')

        approver_id=uuid4();approver_member=await self.member(tenant,approver_id,grants=(RoleGrant('approver','process','reimbursement'),))
        approver=dict(tenant=tenant,person=approver_id,member=approver_member)
        decision=dict(schema_version=1,action='claim.decide',tenant_id=str(tenant),actor_id=str(approver_id),operation_id=str(uuid4()),
            object=dict(kind='claim',id=str(uuid4()),version=1),data=dict(revision=2,stage=1,decision='approve',reason=''))
        decision_request=self.request(decision,'decision');decision_token,decision_claims=await self.authority(approver,decision)
        binding=ClaimBinding(str(tenant),decision['object']['id'],str(person_id),str(approver_id),self.policy_fixture['policy'],'2026-05-01',2,1,draft_id=str(uuid4()))
        self.policies.locked=binding
        require((await self.private(decision_token,decision_request)).status_code==503,'unbound-default-sql-port-denied')
        await self.execute("CREATE OR REPLACE FUNCTION {s}._claim_policy_binding_valid(claims jsonb,request jsonb) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog AS $$ SELECT NULL::boolean; $$")
        require((await self.private(decision_token,decision_request)).status_code==503,'null-sql-port-denied')
        self.passed('locked_claim_metadata_defaults_unavailable_and_NULL_SQL_binding_cannot_bypass_fail_closed_guard')
        # CLAIM-01 has not produced real claim persistence yet. This exact typed
        # snapshot is private owned-fixture state, independently checked again in
        # the eligibility transaction after the Python trusted-port await.
        await self.execute("""CREATE TABLE {s}.fixture_policy_claim_binding(
            tenant_id uuid,claim_id uuid,owner_id uuid,reviewer_id uuid,policy jsonb,trip_date date,revision bigint,stage bigint,active boolean,
            claim_version bigint,draft_id uuid,draft_version bigint);
            ALTER TABLE {s}.fixture_policy_claim_binding ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {s}.fixture_policy_claim_binding FORCE ROW LEVEL SECURITY;
            CREATE POLICY maintenance ON {s}.fixture_policy_claim_binding TO CURRENT_USER USING(true) WITH CHECK(true)""")
        await self.execute('INSERT INTO {s}.fixture_policy_claim_binding VALUES(%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s)',
            (tenant,binding.claim_id,person_id,approver_id,Jsonb(binding.policy),binding.trip_date,binding.revision,binding.stage,binding.claim_version,binding.draft_id,binding.draft_version))
        await self.execute("""CREATE OR REPLACE FUNCTION {s}._claim_policy_binding_valid(claims jsonb,request jsonb) RETURNS boolean
            LANGUAGE sql VOLATILE SET search_path=pg_catalog AS $$
            SELECT EXISTS(SELECT 1 FROM {s}.fixture_policy_claim_binding b WHERE b.active
              AND b.tenant_id=(claims->>'tenant_id')::uuid AND b.claim_id=(claims->>'object_id')::uuid
              AND b.owner_id IS NOT NULL AND request->>'claim_ref'=b.claim_id::text
              AND (claims->>'object_version')::bigint=b.claim_version
              AND request->'policy'=b.policy AND (request->>'trip_date')::date=b.trip_date
              AND (request->'payload'->'data'->>'revision')::bigint=b.revision
              AND ((claims->>'action'='claim.decide' AND b.reviewer_id=(claims->>'sub')::uuid
                AND (request->'payload'->'data'->>'stage')::bigint=b.stage)
                OR (claims->>'action'='claim.information' AND b.owner_id=(claims->>'sub')::uuid
                AND request->'payload'->'data'->'draft'=jsonb_build_object('id',b.draft_id,'version',b.draft_version))) FOR SHARE);
            $$""")
        approved=await self.private(decision_token,decision_request)
        require(approved.status_code==200 and approved.json()['allow'] is True,'approver-exact-claim-metadata-'+str(approved.status_code))
        self.people.append(approver)
        require((await self.source_http(self.policy_fixture['source'],len(self.people)-1)).status_code==404,'approver-metadata-not-source-access')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET active=false')
        require((await self.private(decision_token,decision_request)).status_code==503,'sql-binding-revoke-after-python-snapshot')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET active=true,stage=2')
        require((await self.private(decision_token,decision_request)).status_code==503,'current-sql-stage-drift')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET stage=1,reviewer_id=%s',(person_id,))
        require((await self.private(decision_token,decision_request)).status_code==503,'current-sql-reviewer-drift')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET reviewer_id=%s',(approver_id,))
        self.passed('approver_only_metadata_requires_exact_current_private_claim_binding_and_never_grants_knowledge_content_access')

        information=self.submission(person);information.update(action='claim.information',object=dict(kind='claim',id=binding.claim_id,version=binding.claim_version))
        information['data'].update(revision=binding.revision,draft=dict(id=binding.draft_id,version=binding.draft_version))
        information_request=self.request(information,'information');information_token,_=await self.authority(person,information)
        owner_response=await self.private(information_token,information_request)
        require(owner_response.status_code==200 and owner_response.json()['allow'] is True,'current-claim-owner-information-metadata')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET owner_id=%s',(approver_id,))
        require((await self.private(information_token,information_request)).status_code==503,'owner-drift-after-python-snapshot')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET owner_id=%s,draft_version=draft_version+1',(person_id,))
        require((await self.private(information_token,information_request)).status_code==503,'information-draft-version-drift')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET draft_version=%s,claim_version=claim_version+1',(binding.draft_version,))
        require((await self.private(information_token,information_request)).status_code==503,'information-claim-version-drift')
        await self.execute('UPDATE {s}.fixture_policy_claim_binding SET claim_version=%s',(binding.claim_version,))
        other_person_id=uuid4();other_member=await self.member(tenant,other_person_id,grants=(RoleGrant('employee','tenant',str(tenant)),))
        other_person=dict(tenant=tenant,person=other_person_id,member=other_member)
        wrong_owner=copy.deepcopy(information);wrong_owner.update(actor_id=str(other_person_id),operation_id=str(uuid4()))
        wrong_token,_=await self.authority(other_person,wrong_owner);wrong_request=self.request(wrong_owner,'information')
        require((await self.private(wrong_token,wrong_request)).status_code==403,'non-owner-information-denied-by-port')
        wrong_verified=await self.verified(wrong_token,wrong_owner)
        try:await self.direct_eligibility(wrong_verified,wrong_request)
        except PolicyFailure as error:require(error.status==503,'non-owner-information-denied-by-sql-binding')
        else:raise CheckFailure('non-owner-information-sql-bypass')
        self.passed('information_metadata_binds_exact_current_owner_claim_revision_and_draft_version_in_both_trusted_port_and_locked_SQL_fixture')

        # While eligibility waits for a package lock, its exact claim relation
        # must remain locked. NOWAIT proves a concurrent relation writer cannot
        # slip a stage/reviewer/active change between hook and package/source.
        exact=await self.verified(decision_token,decision)
        pending=None
        try:
            async with self.admin.transaction():
                await self.execute('SELECT id FROM {s}.policy_packages WHERE id=%s FOR UPDATE',(binding.policy['id'],))
                pending=asyncio.create_task(self.direct_eligibility(exact,decision_request))
                await self.wait_for_owned_runtime_lock(pending)
                blocked=False
                try:
                    async with self.admin.transaction():
                        await self.admin.execute(sql.SQL('SELECT claim_id FROM {}.fixture_policy_claim_binding FOR UPDATE NOWAIT').format(sql.Identifier(self.schema)))
                except DatabaseError as error:
                    require(error.sqlstate=='55P03','locked-claim-race-sqlstate');blocked=True
                require(blocked,'claim-relation-not-held-across-package-wait')
            require((await pending)['allow'] is True,'claim-lock-release-result')
        finally:
            if pending is not None and not pending.done():pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        self.passed('exact_current_claim_relation_is_share_locked_until_eligibility_commit_while_package_waits')

        # Simulate the writer's documented coordination->window lock order in
        # the owned fixture. The waiting read must select the committed cutoff.
        future_request=copy.deepcopy(request)
        business_day=await self.scalar('SELECT (clock_timestamp() AT TIME ZONE timezone)::date FROM {s}.tenants WHERE id=%s',(tenant,))
        future_request['trip_date']=(business_day+timedelta(days=10)).isoformat()
        fresh_token,_=await self.authority(person,payload);fresh=await self.verified(fresh_token,payload)
        pending=None
        try:
            async with self.admin.transaction():
                await self.execute('SELECT tenant_id FROM {s}.policy_coordination WHERE tenant_id=%s FOR UPDATE',(tenant,))
                pending=asyncio.create_task(self.direct_eligibility(fresh,future_request))
                await self.wait_for_owned_runtime_lock(pending)
                await self.execute('UPDATE {s}.policy_windows SET ends_on=%s,version=version+1 WHERE package_id=%s',(business_day+timedelta(days=5),binding.policy['id']))
                await self.execute('UPDATE {s}.policy_coordination SET version=version+1 WHERE tenant_id=%s',(tenant,))
            selected=await pending
            require(selected['allow'] is False and selected['blocked_reason']=='not_selected','selection-rechecked-after-coordination-wait')
        finally:
            if pending is not None and not pending.done():pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        self.passed('eligibility_waits_for_selection_writer_and_uses_committed_future_cutoff_without_stale_allow')

        short_token,_=await self.authority(person,payload,lifetime=3);short=await self.verified(short_token,payload)
        pending=None
        try:
            async with self.admin.transaction():
                await self.execute('SELECT tenant_id FROM {s}.policy_coordination WHERE tenant_id=%s FOR UPDATE',(tenant,))
                pending=asyncio.create_task(self.waiting_eligibility(short,request))
                await self.wait_for_owned_runtime_lock(pending)
                await asyncio.sleep(3.1)
            expired=await pending
            require(expired.get('status')=='unauthenticated','original-authority-expired-during-coordination-wait')
        finally:
            if pending is not None and not pending.done():pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        self.passed('original_signed_authority_expires_during_verified_coordination_lock_wait_and_final_RPC_denies_metadata')

        # A policy may stop accepting new claims while preserving its immutable
        # locked-claim eligibility. A real conflict/withdraw holds both uses.
        await self.execute("UPDATE {s}.policy_packages SET allowed_for_new=false,blocked_reason='retired',eligibility_revision=eligibility_revision+1 WHERE id=%s",(binding.policy['id'],))
        new_token,_=await self.authority(person,payload)
        retired=await self.private(new_token,request)
        require(retired.status_code==200 and retired.json()['allow'] is False and retired.json()['blocked_reason']=='retired','retired-denies-new-metadata')
        require((await self.private(decision_token,decision_request)).json()['allow'] is True,'retired-allows-locked-metadata')
        await self.execute("UPDATE {s}.policy_packages SET allowed_for_new=false,allowed_for_locked_claims=false,blocked_reason='known_conflict',eligibility_revision=eligibility_revision+1 WHERE id=%s",(binding.policy['id'],))
        conflicted=await self.private(decision_token,decision_request)
        require(conflicted.status_code==200 and conflicted.json()['allow'] is False and conflicted.json()['blocked_reason']=='known_conflict','conflict-holds-locked')
        await self.execute("UPDATE {s}.policy_packages SET allowed_for_new=true,allowed_for_locked_claims=true,blocked_reason=NULL WHERE id=%s",(binding.policy['id'],))
        require((await self.mutate(self.policy_fixture['source'],'withdraw')).status_code==200,'actual-source-withdrawal')
        withdrawn=await self.private(decision_token,decision_request)
        require(withdrawn.status_code==200 and withdrawn.json()['allow'] is False and withdrawn.json()['blocked_reason']=='source_unavailable','withdrawn-source-holds-locked')
        self.passed('eligibility_distinguishes_retired_new_use_from_locked_use_and_holds_locked_use_on_conflict_or_source_withdrawal')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/pol01/eligibility-proof.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned disposable database proof.')
    try:
        paths=DRAFTS+[Path('scripts/checks/check_policy_draft.py'),Path(__file__)]
        paths+=list(Path('reference_service/src').rglob('*.py'))+[Path('reference_service/tests/unit/test_policy_eligibility_client.py')]
        snapshots={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,harness_type=Harness,target_version=21,operation='policy_eligibility',direct_timeout=240))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in snapshots.items()),'eligibility-source-stable')
        result.update(scope='policy_kernel_and_actual_eligibility_not_whole_acceptance',draft_sha256=snapshots,human_receipts='controlled_typed_fixture_not_ACT01_producer')
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result)
        print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','draft_sha256')}))
    except Exception as error:
        print('Policy eligibility proof failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None


if __name__=='__main__':main()
