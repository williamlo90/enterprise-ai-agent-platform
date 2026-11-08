"""Owned reference4→5 upgrade and candidate-bound service transactions."""
import argparse
import asyncio
import copy
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time
from dataclasses import replace
from uuid import UUID,uuid4
from psycopg import sql
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from check_claims import (
    ROOT,load_settings,DatabaseProbeSettings,parse_target,verify_same_database,Target,schema_and_role,migrate,revisions,
    RuntimeDatabase,direct_connection,mutation_cases,cleanup,require,database_loop_factory,PostgresClaims,PostgresOperations,
    ClaimWork,Result,ActorContext,claims_service,Signer,ClaimPermit,
)


def enrich(prepared):
    identity,actor,packet,cid=prepared;packet=copy.deepcopy(packet)
    packet['permit'].update(candidate_claim_id=cid,operation_key=identity.key)
    for pin in packet['permit']['receipt_pins']:pin['candidate_claim_id']=cid
    return identity,actor,packet,cid


async def run():
    settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings)
    direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);base=parse_target(settings.database_url.get_secret_value());verify_same_database(base,direct)
    admin=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
    schema='ref01_probe_'+uuid4().hex;runtime=replace(admin,user=schema_and_role(schema),password=secrets.token_urlsafe(48))
    db=None;owns=False;phase='source_snapshot'
    paths=list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))+[Path(__file__),ROOT/'scripts/checks/check_claims.py']
    hashes={str(p.resolve().relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report=dict(passed=False,checks=[],scope='service5 candidate wrapper and actual composed service transactions with typed authority fixture; not platform pin API/claim ledger cross-system acceptance',source_sha256=hashes)
    try:
        phase='seed_service4';owns=True
        require(await migrate(admin,runtime,schema=schema,upgrade=True,target_version=4)==(4,4),'owned-service4-prefix')
        db=RuntimeDatabase(runtime,schema,max_size=2);await db.open()
        fixture=await mutation_cases(db,admin,schema,report)
        await db.close();db=None
        phase='upgrade_service5';require(await migrate(admin,runtime,schema=schema,upgrade=True)==(5,5),'owned-service5-upgrade')
        db=RuntimeDatabase(runtime,schema,max_size=2);await db.open()
        async with db.transaction() as connection:
            require((await (await connection.execute(sql.SQL('SELECT {}.runtime_ready()').format(sql.Identifier(schema)))).fetchone())==(True,),'service5-exact-ready')
            require((await (await connection.execute('SELECT has_function_privilege(current_user,%s,\'EXECUTE\')',(schema+'._claim_base_call(text,jsonb)',))).fetchone())==(False,),'old-kernel-private')
        report['checks'].append('owned_service4_history_upgrades_to_service5_exact12function_runtime_and_old_kernel_is_not_executable')
        repo=PostgresClaims(db);ops=PostgresOperations(db)
        async def counts():
            async with direct_connection(admin) as connection:
                return await (await connection.execute(sql.SQL('SELECT (SELECT count(*) FROM {}.claims),(SELECT count(*) FROM {}.service_operations)').format(sql.Identifier(schema),sql.Identifier(schema)))).fetchone()
        phase='candidate_negatives'
        template=enrich(fixture.prepare('claim.submit',fixture.data_for(fixture.proof_expense)))
        before=await counts()
        for change in [lambda p:p['permit'].pop('candidate_claim_id'),lambda p:p['permit'].update(candidate_claim_id=str(uuid4())),
                lambda p:p['permit']['receipt_pins'][0].pop('candidate_claim_id'),lambda p:p['permit']['receipt_pins'][0].update(candidate_claim_id=str(uuid4())),
                lambda p:p['permit'].update(operation_key=str(uuid4())),lambda p:p['permit'].pop('operation_key')]:
            identity,actor,packet,cid=template;packet=copy.deepcopy(packet);change(packet)
            async with db.transaction(ActorContext(UUID(actor.tenant_id),UUID(actor.actor_id))) as uow:
                result=await repo.apply(uow,'submit',packet)
            require(result['status']=='forbidden','candidate-or-key-mismatch-denied')
            require(await counts()==before,'candidate-negative-no-business-or-ledger-effect')
        report['checks'].append('missing_or_wrong_envelope_pin_candidate_or_operation_key_is_denied_before_claim_and_ledger_effects')
        class Authority:
            def __init__(self):self.candidates=[]
            async def authorize(self,payload,actor,*,operation_key,candidate_claim_id,policy,trip_date,claim,context):
                self.candidates.append(candidate_claim_id);stamp=int(time.time());expiry=min(stamp+25,actor.expires_at-1)
                digest=actor.request_hash;op=actor.operation_id;kind={'claim.submit':'submission','claim.information':'information','claim.decide':'decision'}[payload['action']]
                eligibility=dict(allow=True,policy=policy,operation_id=op,request_hash=digest,operation_kind=kind,trip_date=trip_date,
                    claim_ref=None if kind=='submission' else candidate_claim_id,eligibility_revision=1,checked_at=stamp,expires_at=expiry,blocked_reason=None)
                pins=[dict(**r,pin_operation_ref=str(uuid4()),attestation_digest='c'*64,expires_at=expiry,operation_id=op,request_hash=digest,candidate_claim_id=candidate_claim_id) for r in payload['data'].get('receipts',[])]
                return ClaimPermit(json.dumps(dict(request_hash=digest,operation_id=op,operation_key=operation_key,candidate_claim_id=candidate_claim_id,expires_at=expiry,
                    human_receipt_id=str(uuid4()),human_action_ref=str(uuid4()),eligibility=eligibility,receipt_pins=pins)).encode())
        authority=Authority();private=Ed25519PrivateKey.generate()
        signer=Signer(kid='candidate-fixture',private_bytes=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()))
        service=claims_service(db,signer,authority=authority)
        prepared=fixture.prepare('claim.submit',fixture.data_for(fixture.spare));identity,actor,packet,_=prepared
        phase='composed_submit';response=await service.execute(json.loads(packet['canonical']),identity.key,actor,context={'candidate_claim_id':str(uuid4())})
        cid=response['operation']['result']['ref'];row=await repo.read(cid,actor)
        require(row['state']=='pending_approval' and row['version']==1 and authority.candidates[-1]==cid,'candidate-source-submit')
        calls=len(authority.candidates);again=await service.execute(json.loads(packet['canonical']),identity.key,actor)
        require(again['operation']==response['operation'] and len(authority.candidates)==calls,'candidate-committed-replay-no-new-pin')
        report['checks'].append('actual_ClaimsService_derives_candidate_binds_typed_authority_and_commits_shared_ledger_through_service5_wrapper')
        async def commit(prepared,code,version):
            identity,actor,packet,cid=enrich(prepared)
            operation={'claim.submit':'submit','claim.information':'information','claim.decide':'decision'}[identity.kind]
            return await ops.commit(identity,actor,ClaimWork(repo,operation,json.dumps(packet).encode(),Result(cid,version,code)))
        phase='information_and_decisions'
        need=fixture.prepare('claim.decide',dict(revision=1,stage=1,decision='request_information',reason='Fictional additional receipt needed'),claim_id=cid,actor_id=fixture.reviewer,version=1)
        require((await commit(need,'information_requested',2)).state=='committed','candidate-request-information')
        data=fixture.data_for(fixture.spare);data.update(revision=1,draft=dict(id=str(uuid4()),version=1))
        info=fixture.prepare('claim.information',data,claim_id=cid,version=2);ii,ia,ip,_=info
        updated=await service.execute(json.loads(ip['canonical']),ii.key,ia)
        require(updated['operation']['result']['ref']==cid and updated['operation']['result']['version']==3 and authority.candidates[-1]==cid,'candidate-information-same-claim')
        row=await repo.read(cid,ia);require(row['revision']==2 and row['active_stage']==1,'candidate-information-route-reset')
        first=fixture.prepare('claim.decide',dict(revision=2,stage=1,decision='approve',reason=''),claim_id=cid,actor_id=fixture.reviewer,version=3)
        require((await commit(first,'stage_approved',4)).state=='committed','candidate-first-stage')
        second=fixture.prepare('claim.decide',dict(revision=2,stage=2,decision='approve',reason=''),claim_id=cid,actor_id=row['stages'][1]['approver_id'],version=4)
        require((await commit(second,'claim_approved',5)).state=='committed','candidate-last-stage')
        require((await repo.read(cid,ia))['state']=='approved','candidate-terminal-approved')
        report['checks'].append('candidate_bound_information_preserves_claim_resets_revision_and_ordered_decisions_still_commit_under_private_v4_kernel')
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'candidate-source-stable')
        report['passed']=True
    except Exception as error:
        report.update(failure_phase=phase,error_type=type(error).__name__)
        if type(error) is RuntimeError:report['check']=str(error) if str(error).replace('_','').isalnum() else 'sanitized_check'
        current=error
        for _ in range(8):
            if current is None:break
            code=getattr(current,'sqlstate',None)
            if code is not None:
                report['sqlstate']=code;position=getattr(getattr(current,'diag',None),'statement_position',None)
                if str(position).isdigit():report['statement_position']=int(position)
                break
            current=current.__context__
    finally:
        if db is not None:await db.close()
        if owns:await cleanup(admin,runtime,schema)
        report['cleanup']='completed'
    return report


if __name__=='__main__':
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned service schema fixture.')
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(run())
    output=ROOT/'local/file02/candidate-proof.json';output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)
