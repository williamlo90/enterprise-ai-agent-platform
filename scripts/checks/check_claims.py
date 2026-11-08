"""CLAIM-01 owned service namespace checks; no installed schema/AWS/model changes."""
import argparse
import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib
import json
import logging
from pathlib import Path
import secrets
import re
import sys
import time
from uuid import UUID,uuid4
from zoneinfo import ZoneInfo
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend/src'));sys.path.insert(0,str(ROOT/'reference_service/src'))
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.config import load_settings,DatabaseProbeSettings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from reimbursement_service.infrastructure.database.target import Target,schema_and_role
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase,direct_connection
from reimbursement_service.infrastructure.database.migration import migrate,revisions
from reimbursement_service.infrastructure.database.claims import PostgresClaims
from reimbursement_service.infrastructure.database.operations import PostgresOperations
from reimbursement_service.modules.claims.service import ClaimWork
from reimbursement_service.modules.access.ports import VerifiedActor,canonical_payload,request_hash
from reimbursement_service.modules.operations.ports import Identity,Result,OperationFailure
from reimbursement_service.modules.policies.domain import Package,canonical_body,encoded
from reimbursement_service.modules.system.domain import ActorContext
from reimbursement_service.composition import claims_service
from reimbursement_service.modules.claims.domain import ClaimFailure,ClaimPermit
from reimbursement_service.integrations.security.terminal import Signer,Verifier,TrustedKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def uid():return str(uuid4())


async def mutation_cases(database,admin,schema,report):
    tenant,owner,reviewer,reviewer2,trip_id=uid(),uid(),uid(),uid(),uid()
    expense,expense2,expense3,spare,stage_expense,proof_expense,file_id=uid(),uid(),uid(),uid(),uid(),uid(),uid()
    late_trip,late_expense=uid(),uid()
    day=datetime.now(ZoneInfo('Asia/Singapore')).date()
    policy_id=uid()
    body=dict(schema_version=1,digest='0'*64,valid_trip_end_from=(day-timedelta(days=365)).isoformat(),valid_trip_end_to=None,timezone='Asia/Singapore',
        document_versions=[dict(document_id=uid(),version_id=uid(),content_digest='a'*64)],
        rules=dict(currency='SGD',submission_deadline_days=14,max_total_minor=10000,
            categories=[dict(code='travel',receipt_required=True,max_amount_minor=5000)],
            approval_route=[dict(stage=1,approver_id=reviewer,scope_ref='reimbursement'),dict(stage=2,approver_id=reviewer2,scope_ref='reimbursement')]))
    normalized=json.loads(canonical_body(body));normalized.pop('digest')
    body['digest']=hashlib.sha256(b'reimbursement:rule-package:v1:'+encoded(dict(tenant_id=tenant,package_id=policy_id,package_version=1,**normalized))).hexdigest()
    package=Package(tenant,policy_id,1,canonical_body(body))
    policy=dict(id=policy_id,version=1,digest=package.digest)
    async with direct_connection(admin) as connection:
        await connection.execute(sql.SQL('INSERT INTO {}.tenant_registry(tenant_id,provisioning_digest,active) VALUES(%s,%s,true)').format(sql.Identifier(schema)),(tenant,'a'*64))
        await connection.execute(sql.SQL("INSERT INTO {}.trips VALUES(%s,%s,%s,%s,%s,'Fictional employee','completed')").format(sql.Identifier(schema)),(tenant,trip_id,owner,day-timedelta(days=1),day))
        for item in (expense,expense2,expense3,spare,stage_expense,proof_expense):
            await connection.execute(sql.SQL("INSERT INTO {}.trip_expense_items VALUES(%s,%s,%s,%s,%s,'travel',1000,'SGD')").format(sql.Identifier(schema)),(tenant,item,trip_id,owner,day))
        await connection.execute(sql.SQL("INSERT INTO {}.trips VALUES(%s,%s,%s,%s,%s,'Fictional late employee','completed')").format(sql.Identifier(schema)),(tenant,late_trip,owner,day-timedelta(days=16),day-timedelta(days=15)))
        await connection.execute(sql.SQL("INSERT INTO {}.trip_expense_items VALUES(%s,%s,%s,%s,%s,'travel',1000,'SGD')").format(sql.Identifier(schema)),(tenant,late_expense,late_trip,owner,day-timedelta(days=15)))
        await connection.execute(sql.SQL('INSERT INTO {}.service_policy_packages VALUES(%s,%s,1,%s,1,%s,%s,%s,NULL,%s,%s,%s)').format(sql.Identifier(schema)),
            (tenant,policy_id,package.digest,Jsonb(body['rules']),Jsonb(body['document_versions']),body['valid_trip_end_from'],body['timezone'],package.canonical.decode(),int(time.time())))

    claims=PostgresClaims(database);operations=PostgresOperations(database)
    def data_for(item):
        return dict(trip_id=trip_id,currency='SGD',items=[dict(id=uid(),trip_item_id=item,amount_minor=500,receipt_ids=[file_id])],
            total_minor=500,receipts=[dict(id=file_id,version=1,digest='b'*64)],policy=policy,approval_route=copy.deepcopy(body['rules']['approval_route']),destination='reference-reimbursement')

    def prepare(action,data,*,claim_id=None,actor_id=None,version=1,previous=None,trip_day=None):
        now=int(time.time());op=uid();cid=claim_id or uid();person=actor_id or owner
        payload=dict(schema_version=1,action=action,tenant_id=tenant,actor_id=person,operation_id=op,
            object=dict(kind='draft' if action=='claim.submit' else 'claim',id=uid() if action=='claim.submit' else cid,version=version),data=data)
        canonical=canonical_payload(payload);digest=request_hash(payload)
        key=uid();identity=Identity(tenant,op,person,'platform-dispatcher',action,key,digest,payload['object']['kind'],payload['object']['id'],version)
        actor=VerifiedActor(person,tenant,uid(),1,action,op,digest,now,now+55,False)
        kind={'claim.submit':'submission','claim.information':'information','claim.decide':'decision'}[action]
        eligibility=dict(allow=True,policy=policy,operation_id=op,request_hash=digest,operation_kind=kind,trip_date=trip_day or day.isoformat(),claim_ref=None if kind=='submission' else cid,
            eligibility_revision=1,checked_at=now,expires_at=now+55,blocked_reason=None)
        pins=[dict(**r,pin_operation_ref=uid(),attestation_digest='c'*64,expires_at=now+55,operation_id=op,request_hash=digest) for r in data.get('receipts',[])]
        permit=dict(request_hash=digest,operation_id=op,expires_at=now+55,human_receipt_id=uid(),human_action_ref=uid(),eligibility=eligibility,receipt_pins=pins)
        packet=dict(identity=identity.wire(),canonical=canonical.decode(),permit=permit,claim_id=cid,received_at=datetime.now(timezone.utc).isoformat(),previous_claim_id=previous,expires_at=now+55)
        return identity,actor,packet,cid

    async def commit(prepared,code,new_version):
        identity,actor,packet,cid=prepared
        operation={'claim.submit':'submit','claim.information':'information','claim.decide':'decision'}[identity.kind]
        work=ClaimWork(claims,operation,json.dumps(packet,allow_nan=False).encode(),Result(cid,new_version,code))
        return await operations.commit(identity,actor,work)

    first=prepare('claim.submit',data_for(expense));identity,actor,packet,cid=first
    facts=await claims.facts(trip_id,policy,actor)
    require(facts['trip']['owner_principal_ref']==owner and facts['policy']['digest']==package.digest,'actual_owner_trip_and_policy_facts')
    submitted=await commit(first,'claim_submitted',1)
    require(submitted.state=='committed' and submitted.result.ref==cid,'claim_submit_committed')
    row=await claims.read(cid,actor)
    require(row['state']=='pending_approval' and row['version']==1 and len(row['stages'])==2,'actual_claim_header_and_route')
    report['checks'].append('restricted_claim_submission_commits_header_revision_items_receipts_stages_reservation_and_matching_REF03_ledger_audit')

    class NoWork:
        async def apply(self,uow):raise RuntimeError('replayed_work_must_not_run')
    require(await operations.existing(identity,actor)==submitted,'readonly_exact_original_mutation_lookup')
    require(await operations.commit(identity,actor,NoWork())==submitted,'committed_replay_no_effect')
    duplicate=await commit(prepare('claim.submit',data_for(expense)),'claim_submitted',1)
    require(duplicate.state=='rejected' and duplicate.result.code=='business_duplicate','different_line_and_key_still_business_duplicate')
    report['checks'].append('committed_replay_never_executes_feature_and_new_line_UUID_or_operation_key_cannot_duplicate_held_expense')

    first_approval=prepare('claim.decide',dict(revision=1,stage=1,decision='approve',reason='  exact approval reason  '),claim_id=cid,actor_id=reviewer,version=1)
    require((await commit(first_approval,'stage_approved',2)).state=='committed','first_active_stage_approval')
    try:await claims.read(cid,first_approval[1])
    except Exception as error:require(getattr(error,'status',None)==404,'former_reviewer_loses_claim_read')
    else:raise RuntimeError('former_reviewer_must_not_read')
    final=prepare('claim.decide',dict(revision=1,stage=2,decision='approve',reason='x'*3000),claim_id=cid,actor_id=reviewer2,version=2)
    require((await commit(final,'claim_approved',3)).state=='committed','final_active_stage_approval')
    require((await claims.read(cid,actor))['state']=='approved','owner_observes_approved_claim')
    report['checks'].append('ordered_active_reviewer_decisions_commit_same_ledger_and_old_reviewer_loses_access_while_canonical4096_reason_contract_is_preserved')

    # Orphan feature writes cannot commit outside the ledger transaction.
    orphan=prepare('claim.submit',data_for(spare))
    try:
        async with database.transaction(ActorContext(UUID(tenant),UUID(owner)),deadline=orphan[1].expires_at) as uow:
            value=await claims.apply(uow,'submit',orphan[2])
            require(value['status']=='ok','orphan_writes_reach_deferred_guard')
    except Exception as error:
        states=[];current=error
        for _ in range(8):
            if current is None:break
            states.append(getattr(current,'sqlstate',None));current=current.__context__
        require(bool(set(states)&{'23503','23514'}),'orphan_commit_denied_by_deferred_ledger_constraint')
    else:raise RuntimeError('orphan_feature_commit_must_fail')
    async with direct_connection(admin) as connection:
        absent=(await (await connection.execute(sql.SQL('SELECT NOT EXISTS(SELECT 1 FROM {}.claims WHERE id=%s)').format(sql.Identifier(schema)),(orphan[3],))).fetchone())[0]
        require(absent,'orphan_claim_write_rolled_back')
    report['checks'].append('raw_feature_write_without_same_committed_ledger_and_audit_fails_deferred_commit_and_leaves_no_claim')

    for case in ('expired','wrong_operation','wrong_hash','missing_expiry'):
        bad=prepare('claim.submit',data_for(spare));pin=bad[2]['permit']['receipt_pins'][0]
        if case=='expired':pin['expires_at']=int(time.time())-1
        elif case=='wrong_operation':pin['operation_id']=uid()
        elif case=='wrong_hash':pin['request_hash']='d'*64
        else:pin.pop('expires_at')
        try:await commit(bad,'claim_submitted',1)
        except Exception as error:require(getattr(error,'status',None)==400,'pin_binding_invalid_before_effect')
        else:raise RuntimeError('invalid_pin_must_fail')
        require(await operations.existing(bad[0],bad[1]) is None,'bad_pin_no_ledger_outcome')
    report['checks'].append('per_receipt_pin_expiry_operation_and_request_hash_are_required_even_with_valid_aggregate_permit')

    left=prepare('claim.submit',data_for(expense2));right=prepare('claim.submit',data_for(expense2))
    race=await asyncio.gather(commit(left,'claim_submitted',1),commit(right,'claim_submitted',1))
    require(sorted(r.state for r in race)==['committed','rejected'],'two_key_reservation_race_exactly_one_claim')
    winner=left if race[0].state=='committed' else right
    _,race_actor,_,race_claim=winner
    report['checks'].append('two_distinct_operation_keys_race_for_same_authoritative_expense_and_commit_exactly_one_active_claim')

    info_request=prepare('claim.decide',dict(revision=1,stage=1,decision='request_information',reason='Provide more detail'),claim_id=race_claim,actor_id=reviewer,version=1)
    require((await commit(info_request,'information_requested',2)).state=='committed','request_information_committed')
    still_held=await commit(prepare('claim.submit',data_for(expense2)),'claim_submitted',1)
    require(still_held.state=='rejected' and still_held.result.code=='business_duplicate','needs_information_keeps_reservation')
    updated=data_for(expense3);updated.update(revision=1,draft={'id':uid(),'version':2})
    information=prepare('claim.information',updated,claim_id=race_claim,version=2)
    before=await claims.read(race_claim,race_actor)
    try:await commit(information,'information_received',99)
    except Exception as error:require(getattr(error,'status',None)==503,'bad_internal_result_rolls_back_feature')
    else:raise RuntimeError('untrusted_result_must_not_commit')
    require(await claims.read(race_claim,race_actor)==before,'failed_information_restores_old_revision_and_route')
    require(await operations.existing(information[0],information[1]) is None,'failed_information_has_no_ledger_terminal')
    report['checks'].append('failure_after_information_SQL_effects_rolls_back_revision_route_reservation_changes_and_ledger_together')
    require((await commit(information,'information_received',3)).state=='committed','information_revision_committed')
    refreshed=await claims.read(race_claim,race_actor)
    require(refreshed['revision']==2 and refreshed['active_stage']==1 and all(s['state']=='pending' for s in refreshed['stages']),'information_resets_route_approvals')
    released=prepare('claim.submit',data_for(expense2));released_terminal=await commit(released,'claim_submitted',1)
    require(released_terminal.state=='committed','removed_information_item_reservation_released')
    report['checks'].append('information_retains_reservations_until_successful_new_immutable_revision_resets_route_and_atomically_releases_only_removed_items')

    rejected_claim=released[3]
    rejection=prepare('claim.decide',dict(revision=1,stage=1,decision='reject',reason='  '+('x'*3000)+'  '),claim_id=rejected_claim,actor_id=reviewer,version=1)
    require((await commit(rejection,'claim_rejected',2)).state=='committed','reject_releases_items')
    resubmission=await commit(prepare('claim.submit',data_for(expense2),previous=rejected_claim),'claim_submitted',1)
    require(resubmission.state=='committed','rejected_item_can_resubmit_with_previous_link')
    report['checks'].append('actual_rejection_preserves_exact_reason_and_releases_reservations_for_new_submission_with_same_owner_trip_rejected_previous_claim')

    stage_claim=prepare('claim.submit',data_for(stage_expense))
    require((await commit(stage_claim,'claim_submitted',1)).state=='committed','stage_race_claim_created')
    decisions=[prepare('claim.decide',dict(revision=1,stage=1,decision='approve',reason=''),claim_id=stage_claim[3],actor_id=reviewer,version=1) for _ in range(2)]
    outcomes=await asyncio.gather(*(commit(p,'stage_approved',2) for p in decisions))
    require(sorted(v.state for v in outcomes)==['committed','rejected'],'two_keys_only_one_stage_decision')
    require((await claims.read(stage_claim[3],stage_claim[1]))['active_stage']==2,'stage_race_advances_exactly_once')
    report['checks'].append('distinct_decision_operation_keys_race_on_same_claim_version_stage_and_commit_exactly_one_transition')

    for at_boundary in (False,True):
        data=data_for(late_expense);data['trip_id']=late_trip
        late=prepare('claim.submit',data,trip_day=(day-timedelta(days=15)).isoformat())
        if at_boundary:
            # Controlled trusted service timestamp vector; not a real request
            # waiting at midnight. Exact exclusive next-midnight is too late.
            late[2]['received_at']=datetime.combine(day,datetime.min.time(),ZoneInfo('Asia/Singapore')).isoformat()
        terminal=await commit(late,'claim_submitted',1)
        require(terminal.state=='rejected' and terminal.result.code=='deadline_passed','service_SQL_original_calendar_deadline')
    report['checks'].append('service_SQL_rejects_new_submission_after_deadline_and_at_exact_exclusive_tenant_midnight_with_no_effect')

    async with direct_connection(admin) as connection:
        count=(await (await connection.execute(sql.SQL('SELECT count(*) FROM {}.claim_revisions').format(sql.Identifier(schema)))).fetchone())[0]
        require(count>=5,'immutable_revision_history_retained')
        ledger=(await (await connection.execute(sql.SQL('SELECT count(*) FROM {}.service_operations o JOIN {}.service_audit a USING(tenant_id,operation_id) WHERE o.state=a.outcome').format(sql.Identifier(schema),sql.Identifier(schema)))).fetchone())[0]
        total=(await (await connection.execute(sql.SQL('SELECT count(*) FROM {}.service_operations').format(sql.Identifier(schema)))).fetchone())[0]
        require(ledger==total==17,'all_terminal_outcomes_have_exact_audit')
        reason=(await (await connection.execute(sql.SQL('SELECT reason FROM {}.decisions WHERE operation_id=%s').format(sql.Identifier(schema)),(rejection[0].operation_id,))).fetchone())[0]
        require(reason=='  '+('x'*3000)+'  ','exact_reason_not_trimmed_or_truncated')
        checks=[('immutable_revision',sql.SQL('UPDATE {}.claim_revisions SET total_minor=1 WHERE claim_id=%s').format(sql.Identifier(schema)),(cid,)),
                ('immutable_decision',sql.SQL("UPDATE {}.decisions SET reason='changed' WHERE claim_id=%s").format(sql.Identifier(schema)),(cid,)),
                ('terminal_header',sql.SQL('UPDATE {}.claims SET version=version+1 WHERE id=%s').format(sql.Identifier(schema)),(cid,)),
                ('pending_stage_not_null',sql.SQL('UPDATE {}.claims SET active_stage_no=NULL,version=version+1 WHERE id=%s').format(sql.Identifier(schema)),(race_claim,))]
        for label,query,args in checks:
            try:
                async with connection.transaction():await connection.execute(query,args)
            except Exception as error:require(getattr(error,'sqlstate',None)=='23514',label)
            else:raise RuntimeError('protected_history_or_state_change_must_fail')
    report['checks'].append('revision_decision_history_and_terminal_audits_remain_consistent_and_reason_whitespace_is_not_rewritten')
    report['checks'].append('immutable_revisions_decisions_terminal_claim_headers_and_nonnull_active_pending_stage_constraints_reject_direct_owned_fixture_tampering')

    # An active tenant B and a valid actor context still cannot read A's claim.
    foreign=uid()
    async with direct_connection(admin) as connection:
        await connection.execute(sql.SQL('INSERT INTO {}.tenant_registry(tenant_id,provisioning_digest,active) VALUES(%s,%s,true)').format(sql.Identifier(schema)),(foreign,'e'*64))
    for wrong in (replace(actor,tenant_id=foreign),replace(actor,actor_id=uid())):
        try:await claims.read(cid,wrong)
        except Exception as error:require(getattr(error,'status',None)==404,'foreign_claim_hidden_from_current_actor')
        else:raise RuntimeError('foreign_claim_read_must_fail')
    report['checks'].append('active_foreign_tenant_and_same_tenant_nonowner_context_cannot_read_other_claim')
    async with direct_connection(admin) as connection:
        await connection.execute(sql.SQL('UPDATE {}.tenant_registry SET active=false WHERE tenant_id=%s').format(sql.Identifier(schema)),(tenant,))
        try:
            try:await claims.read(cid,actor)
            except Exception as error:require(getattr(error,'status',None)==404,'inactive_local_tenant_denies_claim_read')
            else:raise RuntimeError('inactive_tenant_claim_read_must_fail')
        finally:
            await connection.execute(sql.SQL('UPDATE {}.tenant_registry SET active=true WHERE tenant_id=%s').format(sql.Identifier(schema)),(tenant,))
    try:await claims.read(cid,replace(actor,expires_at=int(time.time())+1000))
    except Exception as error:require(getattr(error,'status',None)==401,'claim_capability_rejects_extended_authority')
    else:raise RuntimeError('extended_authority_must_fail')
    report['checks'].append('disabled_local_tenant_and_authority_beyond65_second_protocol_bound_fail_closed_even_for_owner')
    return SimpleNamespace(prepare=prepare,data_for=data_for,commit=commit,tenant=tenant,owner=owner,reviewer=reviewer,
        trip_id=trip_id,policy=policy,day=day,spare=spare,proof_expense=proof_expense,late_trip=late_trip,late_expense=late_expense)


async def composed_cases(database,admin,schema,fixture,report):
    """Actual root/service/repos/UoW and terminal crypto; typed source fixtures."""
    key=Ed25519PrivateKey.generate();now=int(time.time())
    signer=Signer(kid='claim-fixture',private_bytes=key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()))
    verifier=Verifier({'claim-fixture':TrustedKey(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw),now-60,now+3600)})
    active=[0];original=database.transaction
    @asynccontextmanager
    async def observed(context=None,*,deadline=None):
        async with original(context,deadline=deadline) as uow:
            active[0]+=1
            try:yield uow
            finally:active[0]-=1
    database.transaction=observed

    class Authority:
        def __init__(self):self.calls=0;self.available=True;self.malformed=False;self.last_expiry=None
        async def authorize(self,payload,actor,*,policy,trip_date,claim,context):
            require(active[0]==0,'no_database_transaction_held_during_external_authority')
            self.calls+=1
            if not self.available:raise ClaimFailure(503)
            stamp=int(time.time());expiry=min(stamp+15,actor.expires_at-1);self.last_expiry=expiry
            digest=request_hash(payload);op=payload['operation_id']
            kind={'claim.submit':'submission','claim.information':'information','claim.decide':'decision'}[payload['action']]
            elig=dict(allow=True,policy=policy,operation_id=op,request_hash=digest,operation_kind=kind,trip_date=trip_date,
                claim_ref=None if kind=='submission' else payload['object']['id'],eligibility_revision=1,checked_at=stamp,
                expires_at=min(stamp+50,actor.expires_at),blocked_reason=None)
            pins=[dict(**r,pin_operation_ref=uid(),attestation_digest='c'*64,expires_at=expiry,operation_id=op,request_hash=digest) for r in payload['data'].get('receipts',[])]
            value=dict(request_hash=digest,operation_id=op,expires_at=expiry,human_receipt_id=uid(),human_action_ref=uid(),eligibility=elig,receipt_pins=pins)
            if self.malformed:value['request_hash']='0'*64
            return ClaimPermit(json.dumps(value).encode())

    authority=Authority();service=claims_service(database,signer,authority=authority)
    async def invoke(prepared,target=service):
        identity,actor,packet,_=prepared
        response=await target.execute(json.loads(packet['canonical']),identity.key,actor)
        verified=verifier.verify(response['proof'],identity)
        require(verified.wire()==response['operation'],'actual_terminal_signature_matches_committed_ledger')
        return response
    async def counts():
        async with direct_connection(admin) as connection:
            return (await (await connection.execute(sql.SQL('SELECT (SELECT count(*) FROM {}.claims),(SELECT count(*) FROM {}.service_operations)').format(sql.Identifier(schema),sql.Identifier(schema)))).fetchone())
    try:
        before=await counts();denied=fixture.prepare('claim.submit',fixture.data_for(fixture.spare))
        try:await invoke(denied,claims_service(database,signer))
        except ClaimFailure as error:require(error.status==503,'default_denied_authority')
        else:raise RuntimeError('unbound_authority_must_not_write')
        require(await counts()==before,'default_deny_no_claim_or_ledger_effect')
        report['checks'].append('actual_claim_composition_defaults_to_denied_authority_and_creates_no_claim_or_terminal_for_new_request')

        submitted=fixture.prepare('claim.submit',fixture.data_for(fixture.spare))
        response=await invoke(submitted)
        cid=response['operation']['result']['ref']
        require(response['operation']['state']=='committed' and response['operation']['result']['code']=='claim_submitted','actual_composed_submission')
        require(response['proof']['payload']['expires_at']<=authority.last_expiry,'terminal_proof_bounded_to_validated_permit')
        report['checks'].append('actual_ClaimsService_factory_validates_typed_permit_before_shared_UoW_and_signs_committed_result_with_clamped_authority')

        rejection=fixture.prepare('claim.decide',dict(revision=1,stage=1,decision='reject',reason='Controlled resubmission fixture'),claim_id=cid,actor_id=fixture.reviewer,version=1)
        require((await invoke(rejection))['operation']['result']['code']=='claim_rejected','composed_rejection')
        resubmission=fixture.prepare('claim.submit',fixture.data_for(fixture.spare))
        resubmitted=await invoke(resubmission);new_id=resubmitted['operation']['result']['ref']
        async with direct_connection(admin) as connection:
            previous=(await (await connection.execute(sql.SQL('SELECT previous_claim_id::text FROM {}.claims WHERE id=%s').format(sql.Identifier(schema)),(new_id,))).fetchone())[0]
        require(previous==cid,'previous_claim_derived_from_actual_owned_rejected_item_history')
        report['checks'].append('actual_composed_resubmission_derives_previous_rejected_link_from_SQL_history_without_unsigned_browser_field')

        # Seed an existing historical received-time vector through real UoW.
        # This is a controlled record fixture, not a claim that wall time moved.
        late_data=fixture.data_for(fixture.late_expense);late_data['trip_id']=fixture.late_trip
        historical=fixture.prepare('claim.submit',late_data,trip_day=(fixture.day-timedelta(days=15)).isoformat())
        historical[2]['received_at']=(datetime.combine(fixture.day,datetime.min.time(),ZoneInfo('Asia/Singapore'))-timedelta(microseconds=1)).isoformat()
        require((await fixture.commit(historical,'claim_submitted',1)).state=='committed','historical_received_record_fixture')
        old_calls=authority.calls;authority.available=False
        replayed=await invoke(historical)
        require(replayed['operation']['result']['ref']==historical[3] and authority.calls==old_calls,'existing_after_deadline_skips_new_authority_and_deadline')
        authority.available=True
        report['checks'].append('actual_composed_retry_of_historical_received_record_after_deadline_returns_original_terminal_without_new_authority_preparation')

        class UnavailableSigner:
            def issue(self,*args,**kwargs):raise OperationFailure(503)
        lost=fixture.prepare('claim.submit',fixture.data_for(fixture.proof_expense))
        service.operations.proofs=UnavailableSigner()
        try:await invoke(lost)
        except OperationFailure as error:require(error.status==503,'post_commit_proof_unavailable')
        else:raise RuntimeError('missing_terminal_proof_must_not_report_success')
        service.operations.proofs=signer;authority.available=False;old_calls=authority.calls
        recovered=await invoke(lost)
        require(recovered['operation']['state']=='committed' and authority.calls==old_calls,'post_commit_recovery_skips_new_effect_and_dependencies')
        authority.available=True
        report['checks'].append('post_commit_terminal_signing_failure_is_recovered_by_exact_read_only_replay_without_repeating_claim_effect_or_pin_authority')

        before=await counts();authority.malformed=True
        bad=fixture.prepare('claim.submit',fixture.data_for(fixture.spare))
        try:await invoke(bad)
        except ClaimFailure as error:require(error.status==503,'malformed_typed_authority_denied')
        else:raise RuntimeError('malformed_authority_must_not_write')
        require(await counts()==before,'malformed_authority_no_claim_or_ledger_effect')
        report['checks'].append('actual_composed_malformed_authority_binding_fails_before_any_business_or_terminal_write')
        require(active[0]==0,'all_composed_uows_closed')
    finally:database.transaction=original


def require(ok,label):
    if not ok:raise RuntimeError(label)


async def cleanup(admin,runtime,schema):
    async with direct_connection(admin) as connection:
        namespace=await (await connection.execute('SELECT oid FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()
        role=await (await connection.execute('SELECT oid FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone()
        if namespace is None:
            require(role is None,'no_unbound_owned_role');return
        require(role is not None,'owned_role_missing')
        binding=await (await connection.execute(sql.SQL('SELECT role_name,role_oid FROM {}.runtime_binding').format(sql.Identifier(schema)))).fetchall()
        require(binding==[(runtime.user,role[0])],'owned_schema_role_binding')
        await connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        dbname=(await (await connection.execute('SELECT current_database()')).fetchone())[0]
        await connection.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(sql.Identifier(dbname),sql.Identifier(runtime.user)))
        await connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(runtime.user)))
        require((await (await connection.execute('SELECT count(*) FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone())[0]==0,'owned_schema_removed')


async def run():
    settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings)
    direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
    base=parse_target(settings.database_url.get_secret_value());verify_same_database(base,direct)
    admin=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
    schema='ref01_probe_'+uuid4().hex
    runtime=replace(admin,user=schema_and_role(schema),password=secrets.token_urlsafe(48))
    database=None;owns_attempt=False;phase='preflight'
    manifest=revisions();snapshots={r.name:r.checksum for r in manifest}
    files=list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))+[Path(__file__).resolve()]
    source={str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    report={'passed':False,'scope':'service4 schema and actual restricted claim/ledger transactions; controlled human/receipt/policy permits, no FILE/ACT/FLOW producer or HTTP/cloud acceptance',
            'checks':[],'migration_sha256':snapshots,'source_sha256':source,'installed_schema_modified':False,'actual_paid_inference_calls':0}
    try:
        async with direct_connection(admin) as connection:
            require(not await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone(),'fresh_namespace')
            require(not await (await connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(runtime.user,))).fetchone(),'fresh_role')
        owns_attempt=True;phase='migration'
        require(await migrate(admin,runtime,schema=schema,upgrade=True)==(4,4),'service4_migrated')
        report['checks'].append('forward_only_service4_schema_and_capabilities_compile')
        phase='restricted_runtime_open';database=RuntimeDatabase(runtime,schema,max_size=2);await database.open()
        async with database.transaction() as connection:
            ready=(await (await connection.execute(sql.SQL('SELECT {}.runtime_ready()').format(sql.Identifier(schema)))).fetchone())[0]
            require(ready is True,'exact_service4_runtime_manifest')
        report['checks'].append('restricted_runtime_validates_exact16tables_11functions_and_no_private_table_privileges')
        phase='claim_mutations';fixture=await mutation_cases(database,admin,schema,report)
        phase='composed_claims_service';await composed_cases(database,admin,schema,fixture,report)
        require({r.name:r.checksum for r in revisions()}==snapshots,'migration_source_stable')
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in source.items()),'source_stable')
        report['passed']=True
    except Exception as error:
        # Only stage and class names; provider/driver exception text can contain
        # identifiers or credentials and is never persisted or printed.
        report.update(failure_phase=phase,error_type=type(error).__name__)
        if type(error) is RuntimeError and re.fullmatch('[a-z0-9_]{1,120}',str(error)):report['failure_check']=str(error)
        current=error
        for _ in range(8):
            if current is None:break
            code=getattr(current,'sqlstate',None);diag=getattr(current,'diag',None)
            if isinstance(code,str) and re.fullmatch('[A-Z0-9]{5}',code):
                report['sqlstate']=code
                position=getattr(diag,'statement_position',None)
                if str(position).isdigit():report['statement_position']=int(position)
                context=getattr(diag,'context','') or ''
                frames=re.findall(r'(?:function|FUNCTION) [\"]?([a-z_]+)[\"]?(?:[^\n]*?) (?:line|LINE) ([0-9]+)',context)
                report['sql_frames']=[{'function':f,'line':int(n)} for f,n in frames]
                primary=getattr(diag,'message_primary','') or ''
                report['error_signals']={s:s in primary.lower() for s in ('syntax','ambiguous','does not exist','constraint','record variable','into list','already exists')}
                break
            current=current.__context__
    finally:
        try:
            if database is not None:await database.close()
        finally:
            if owns_attempt:await cleanup(admin,runtime,schema)
        report['cleanup']='completed'
    folder=ROOT/'local/claim01';folder.mkdir(parents=True,exist_ok=True)
    (folder/'schema-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned database fixture.')
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:raise SystemExit(runner.run(run()))
