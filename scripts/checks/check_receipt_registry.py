"""Owned restricted database receipt registry proof; controlled ACT source only."""
import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import re
from pathlib import Path
import secrets
import sys
import time
import traceback
from uuid import uuid4
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
from psycopg import sql,Error as DatabaseError
from psycopg.types.json import Jsonb
from check_policy_eligibility import Harness as AuthorityHarness,SigningFixture,Receipts,ISSUER,AUDIENCE,CALLER
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import load_settings,DatabaseProbeSettings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.delegation import PostgresDelegationRepository
from enterprise_ai_platform.infrastructure.database.receipts import PostgresReceiptRepository
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier
from enterprise_ai_platform.integrations.security.receipt_terminal import ReceiptTerminalVerifier,TerminalKey
from enterprise_ai_platform.integrations.security.receipt_settlement import ReceiptSettlement,SettlementKey
from enterprise_ai_platform.modules.delegation.service import DelegationService
from enterprise_ai_platform.modules.receipts.service import ReceiptPinService
from enterprise_ai_platform.modules.receipts.domain import PinBatch,PinIdentity,receipt_refs,ReceiptFailure,encoded
from enterprise_ai_platform.modules.delegation.domain import canonical_payload
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'reference_service/src'))
from reimbursement_service.integrations.security.terminal import Signer
from reimbursement_service.modules.operations.domain import Identity,Terminal,Result


class Harness(AuthorityHarness):
    async def registry(self):
        self.passed('packaged_schema22_and_exact_receipt_capabilities_compile_under_restricted_runtime')
        async with self.database.transaction() as connection:
            require((await (await connection.execute(sql.SQL('SELECT {}.runtime_role_is_valid()').format(sql.Identifier(self.schema)))).fetchone())==(True,),'receipt-runtime-valid')
            for name in ('receipt_receiver_keys','receipt_pin_batches','receipt_pins'):
                require((await (await connection.execute('SELECT has_table_privilege(current_user,%s,\'SELECT,INSERT,UPDATE,DELETE\')',(self.schema+'.'+name,))).fetchone())==(False,),'receipt-private-'+name)
        self.passed('runtime_cannot_read_private_keys_or_read_write_pin_registry_directly')
        self.signing=SigningFixture();self.receipts=Receipts();self.credential=secrets.token_urlsafe(32)
        self.credential_hash=hashlib.sha256(('eai:internal-caller:v1:'+self.credential).encode()).hexdigest()
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'receipt-fixture',%s,%s,%s,%s,clock_timestamp()+interval '1 hour')",
            (uuid4(),self.credential_hash,AUDIENCE,CALLER,Jsonb(['claim.submit','claim.information'])))
        await self.execute("INSERT INTO {s}.delegation_keys VALUES(%s,'current',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(ISSUER,Jsonb(self.signing.jwk)))
        self.delegation=DelegationService(PostgresDelegationRepository(self.database),RSADelegationVerifier(),ISSUER,receipts=self.receipts)
        repo=PostgresReceiptRepository(self.database);service=ReceiptPinService(repo,self.delegation)
        self.policy_fixture=dict(policy=dict(id=str(uuid4()),version=1,digest='a'*64),content=dict(rules=dict(approval_route=[dict(stage=1,approver_id=str(self.people[1]['person']),scope_ref='reimbursement')])))
        async def command(index=0,key=None):
            person=self.people[index]
            uploaded=await self.upload(b'%PDF-1.4\nFictional receipt registry fixture',index=index,purpose='receipt',mime='application/pdf')
            require(uploaded.status_code==202,'receipt-upload-admitted');await self.process()
            f=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
            require(f['scan_state']=='ready','receipt-real-worker-ready')
            payload=self.submission(person);payload['data']['receipts']=[dict(id=f['id'],version=f['version'],digest=f['digest'])]
            payload['data']['items']=[dict(id=str(uuid4()),trip_item_id=str(uuid4()),amount_minor=100,receipt_ids=[f['id']])];payload['data']['total_minor']=100
            token,claims=await self.authority(person,payload)
            values=dict(payload=payload,key=key or str(uuid4()),candidate_claim_id=str(uuid4()))
            return values,token,claims,f
        values,token,claims,file=await command()
        try:await service.prepare(self.credential,token,values)
        except ReceiptFailure as error:require(error.status==503,'unbound-human-source-denies')
        else:raise CheckFailure('unbound-human-must-deny')
        require(await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')==0,'unbound-no-pin')
        self.passed('unbound_transaction_local_human_source_denies_despite_valid_signed_delegation')
        # Owned ACT fixture has exact original signed claims, row locks and
        # revocation. It is never provisioned into the installed application.
        await self.execute('CREATE TABLE {s}.receipt_human_fixture(id uuid PRIMARY KEY,claims jsonb NOT NULL,revoked boolean NOT NULL DEFAULT false)')
        await self.execute('REVOKE ALL ON {s}.receipt_human_fixture FROM PUBLIC')
        await self.execute('''CREATE OR REPLACE FUNCTION {s}._receipt_human_current(claims jsonb,payload jsonb) RETURNS boolean
          LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog AS $f$
          #variable_conflict use_variable
          DECLARE r record;BEGIN SELECT * INTO r FROM {s}.receipt_human_fixture WHERE id=(claims->>'human_receipt_id')::uuid FOR SHARE;
          RETURN r.id IS NOT NULL AND NOT r.revoked AND r.claims=claims;END $f$''')
        await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(claims['human_receipt_id'],Jsonb(claims)))
        root=secrets.token_bytes(32);now=int(time.time())
        await self.execute("INSERT INTO {s}.receipt_receiver_keys VALUES(%s,'fixture',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 hour')",(self.credential_hash,root))
        settlement=ReceiptSettlement(SettlementKey('fixture',root,now-60,now+3600))
        private=Ed25519PrivateKey.generate()
        signer=Signer(kid='fixture',private_bytes=private.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption()))
        verifier=ReceiptTerminalVerifier({'fixture':TerminalKey(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),now-60,now+3600)})
        def proof(batch,state):
            moment=int(time.time());identity=Identity(**batch.identity.wire())
            terminal=Terminal(identity,state,Result(batch.claim_id,1,'claim_submitted') if state=='committed' else Result(None,None,'sealed_no_commit'),moment)
            return verifier.verify(signer.issue(terminal,authority_deadline=moment+40),batch)
        prepared=await service.prepare(self.credential,token,values)
        again=await service.prepare(self.credential,token,values)
        require(prepared['batch_digest']==again['batch_digest'] and await self.scalar('SELECT count(*) FROM {s}.receipt_pins')==1,'same-pin-replay')
        loaded=await repo.lookup(self.credential_hash,values['payload']['tenant_id'],values['payload']['operation_id'])
        batch=PinBatch(PinIdentity(**loaded['batch']['identity']),loaded['batch']['claim_id'],receipt_refs(loaded['batch']['receipts']))
        require(loaded['state']=='held' and batch.digest==prepared['batch_digest'],'actual-held-batch')
        self.passed('actual_private_delegation_current_human_ready_owner_file_and_full_batch_bind_to_one_durable_replayable_pin')
        altered=copy.deepcopy(values);altered['candidate_claim_id']=str(uuid4())
        try:await service.prepare(self.credential,token,altered)
        except ReceiptFailure as error:require(error.status==409,'candidate-rebind-conflict')
        else:raise CheckFailure('candidate-rebinding-must-fail')
        require((await repo.lookup(self.credential_hash,values['payload']['tenant_id'],values['payload']['operation_id']))['state']=='held','conflict-keeps-held')
        self.passed('same_operation_cannot_rebind_candidate_claim_or_replace_held_material')
        foreign,foreign_token,foreign_claims,_=await command(index=4,key=values['key'])
        await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(foreign_claims['human_receipt_id'],Jsonb(foreign_claims)))
        require((await service.prepare(self.credential,foreign_token,foreign))['state']=='held','two-owner-same-key-valid')
        duplicate,duplicate_token,duplicate_claims,_=await command(key=values['key'])
        await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(duplicate_claims['human_receipt_id'],Jsonb(duplicate_claims)))
        try:await service.prepare(self.credential,duplicate_token,duplicate)
        except ReceiptFailure as error:require(error.status==409,'same-actor-key-conflict')
        else:raise CheckFailure('same-actor-key-must-not-create-second-batch')
        require(await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')==2,'duplicate-key-no-orphan-batch')
        self.passed('same_idempotency_key_is_independent_for_two_owners_but_same_actor_key_cannot_create_another_operation')
        async def deletion(allowed):
            try:
                async with self.admin.transaction():
                    await self.admin.execute(sql.SQL("UPDATE {}.files SET deletion_state='deletion_pending' WHERE id=%s").format(sql.Identifier(self.schema)),(file['id'],))
            except DatabaseError as error:require(not allowed and error.sqlstate=='23514','held-deletion-guard')
            else:require(allowed,'unexpected-retained-file-deletion')
        await deletion(False)
        envelope=settlement.issue(batch,proof(batch,'sealed_no_commit'));fake=copy.deepcopy(envelope);fake['proof']='0'*64
        try:await repo.finalise(self.credential_hash,fake)
        except ReceiptFailure as error:require(error.status==403,'forged-settlement-denied')
        else:raise CheckFailure('forged-settlement-must-fail')
        require((await repo.lookup(self.credential_hash,values['payload']['tenant_id'],values['payload']['operation_id']))['state']=='held','forged-keeps-held')
        self.passed('held_file_cannot_be_deleted_and_fabricated_terminal_dictionary_without_private_MAC_cannot_release_it')
        done=await repo.finalise(self.credential_hash,envelope);retry=await repo.finalise(self.credential_hash,envelope)
        require(done['state']==retry['state']=='released','exact-seal-release');await deletion(True)
        self.passed('authentic_REF03_seal_verified_in_Python_and_private_SQL_MAC_releases_once_and_permits_storage_deletion_state')
        values2,token2,claims2,file2=await command()
        await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(claims2['human_receipt_id'],Jsonb(claims2)))
        await service.prepare(self.credential,token2,values2)
        row=await repo.lookup(self.credential_hash,values2['payload']['tenant_id'],values2['payload']['operation_id'])
        batch2=PinBatch(PinIdentity(**row['batch']['identity']),row['batch']['claim_id'],receipt_refs(row['batch']['receipts']))
        retained=await repo.finalise(self.credential_hash,settlement.issue(batch2,proof(batch2,'committed')))
        require(retained['state']=='finalised' and retained['claim_version']==1,'committed-retention')
        try:await repo.finalise(self.credential_hash,settlement.issue(batch2,proof(batch2,'sealed_no_commit')))
        except ReceiptFailure as error:require(error.status==409,'terminal-conflict')
        else:raise CheckFailure('finalised-must-not-release')
        file=file2;await deletion(False)
        self.passed('authentic_committed_claim_retains_exact_candidate_version_and_conflicting_release_cannot_change_finalised_history')
        values3,token3,claims3,file3=await command()
        await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(claims3['human_receipt_id'],Jsonb(claims3)))
        frozen=json.loads(canonical_payload(values3['payload']));original=frozen['object']
        batch3=PinBatch(PinIdentity(frozen['tenant_id'],frozen['operation_id'],frozen['actor_id'],'platform-dispatcher',frozen['action'],values3['key'],
            claims3['request_hash'],original['kind'],original['id'],original['version']),values3['candidate_claim_id'],receipt_refs(frozen['data']['receipts']))
        verified=await self.verified(token3,frozen)
        async def denied_prepare(reference,status):
            before=await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')
            try:await repo.prepare(batch3,frozen,reference,dict(verified.actor))
            except ReceiptFailure as error:require(error.status==status,'source-rejection-'+str(status))
            else:raise CheckFailure('revoked-source-must-deny')
            require(await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')==before,'source-denial-no-pin')
        spliced={**verified.reference.wire(),'jti':str(uuid4())};await denied_prepare(spliced,401)
        await self.execute('UPDATE {s}.receipt_human_fixture SET revoked=true WHERE id=%s',(claims3['human_receipt_id'],))
        await denied_prepare(verified.reference.wire(),503)
        await self.execute('UPDATE {s}.receipt_human_fixture SET revoked=false WHERE id=%s',(claims3['human_receipt_id'],))
        await self.execute('UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE id=%s',(claims3['session_id'],))
        await denied_prepare(verified.reference.wire(),401)
        await self.execute('UPDATE {s}.sessions SET revoked_at=NULL WHERE id=%s',(claims3['session_id'],))
        self.passed('spliced_original_verification_reference_revoked_human_source_or_revoked_session_cannot_create_a_pin')
        await service.prepare(self.credential,token3,values3)
        old_expiry=await self.scalar('SELECT authority_expires_at FROM {s}.receipt_pin_batches WHERE operation_id=%s',(frozen['operation_id'],))
        try:
            async with self.admin.transaction():
                await self.admin.execute(sql.SQL("UPDATE {}.receipt_pin_batches SET authority_expires_at=clock_timestamp()+interval '0.5 seconds' WHERE operation_id=%s").format(sql.Identifier(self.schema)),(frozen['operation_id'],))
                await asyncio.sleep(.7)
        except DatabaseError as error:require(error.sqlstate=='40001','deferred-receipt-expiry')
        else:raise CheckFailure('expired-receipt-transaction-must-not-commit')
        require(await self.scalar('SELECT authority_expires_at FROM {s}.receipt_pin_batches WHERE operation_id=%s',(frozen['operation_id'],))==old_expiry,'expired-update-rolled-back')
        self.passed('deferred_commit_expiry_rejects_after_statement_authority_loss_and_rolls_back_pin_metadata')
        valid_settlement=settlement.issue(batch3,proof(batch3,'sealed_no_commit'))
        await self.execute('UPDATE {s}.receipt_receiver_keys SET active=false')
        try:await repo.finalise(self.credential_hash,valid_settlement)
        except ReceiptFailure as error:require(error.status==403,'revoked-settlement-key')
        else:raise CheckFailure('revoked-settlement-key-must-not-release')
        await self.execute('UPDATE {s}.receipt_receiver_keys SET active=true')
        await self.execute('UPDATE {s}.internal_caller_credentials SET active=false WHERE credential_hash=%s',(self.credential_hash,))
        try:await repo.finalise(self.credential_hash,valid_settlement)
        except ReceiptFailure as error:require(error.status==401,'revoked-caller')
        else:raise CheckFailure('revoked-caller-must-not-release')
        await self.execute('UPDATE {s}.internal_caller_credentials SET active=true WHERE credential_hash=%s',(self.credential_hash,))
        expired=copy.deepcopy(valid_settlement);expired['material']['terminal'].update(completed_at=int(time.time())-2,expires_at=int(time.time())-1)
        expired['proof']=hmac.new(root,b'eai:receipt-settlement:v1:'+encoded(expired['material']),hashlib.sha256).hexdigest()
        try:await repo.finalise(self.credential_hash,expired)
        except ReceiptFailure as error:require(error.status==401,'expired-authentic-mac')
        else:raise CheckFailure('expired-mac-must-not-release')
        require((await repo.lookup(self.credential_hash,frozen['tenant_id'],frozen['operation_id']))['state']=='held','revoked-or-expired-keeps-held')
        self.passed('revoked_private_key_revoked_caller_or_expired_correct_MAC_never_releases_a_held_pin')
        # A controlled signed authority genuinely expires while the restricted
        # transaction waits on the original ready file, before its final check.
        short_token,short_claims=await self.authority(self.people[0],frozen,lifetime=6)
        await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(short_claims['human_receipt_id'],Jsonb(short_claims)))
        short=await self.verified(short_token,frozen)
        auth=dict(actor=dict(short.actor),credential_hash=self.credential_hash,verification_ref=short.reference.wire())
        async def waiting():
            async with self.database.transaction() as connection:
                await connection.execute("SET LOCAL lock_timeout='8000ms'")
                await connection.execute("SET LOCAL statement_timeout='10000ms'")
                return (await (await connection.execute(sql.SQL('SELECT {}.receipt_pin_prepare(%s,%s)').format(sql.Identifier(self.schema)),(Jsonb(auth),Jsonb(dict(batch=batch3.wire(),payload=frozen))))).fetchone())[0]
        pending=None
        try:
            async with self.admin.transaction():
                await self.execute('SELECT id FROM {s}.files WHERE id=%s FOR UPDATE',(file3['id'],))
                pending=asyncio.create_task(waiting());await self.wait_for_owned_runtime_lock(pending)
                await asyncio.sleep(max(0,short.actor['expires_at']-time.time())+.1)
            require((await pending)['status']=='unauthenticated','original-authority-expired-at-file-lock')
        finally:
            if pending is not None and not pending.done():pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        require((await repo.lookup(self.credential_hash,frozen['tenant_id'],frozen['operation_id']))['state']=='held','expired-refresh-keeps-original-held')
        self.passed('original_signed_authority_expiring_during_observed_file_lock_wait_cannot_refresh_a_pin')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned database fixture.')
    output=Path('local/file02/registry-proof.json');output.parent.mkdir(parents=True,exist_ok=True)
    report={'passed':False}
    try:
        helper_paths=[Path(__file__),Path('scripts/checks/check_policy_eligibility.py'),Path('scripts/checks/check_policy_draft.py'),Path('scripts/checks/check_knowledge_workspace.py'),
            Path('reference_service/src/reimbursement_service/integrations/security/terminal.py'),Path('reference_service/src/reimbursement_service/modules/operations/domain.py')]
        helpers={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in helper_paths}
        settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=22,operation='registry',direct_timeout=240))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in helpers.items()),'registry-helper-source-stable')
        report['helper_sha256']=helpers
        report.update(scope='actual restricted receipt registry transactions; ACT row source/storage/parser controlled, no HTTP/download or whole FILE02 acceptance')
    except Exception as error:
        report.update(error_type=type(error).__name__)
        if isinstance(error,CheckFailure):report['check']=str(error)
        current=error
        frames=[]
        for _ in range(10):
            if current is None:break
            frames.extend(dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__))
            if type(getattr(current,'status',None)) is int:report['dependency_status']=current.status
            if isinstance(current,DatabaseError):
                report['sqlstate']=current.sqlstate
                position=getattr(current.diag,'statement_position',None)
                if str(position).isdigit():report['statement_position']=int(position)
                primary=current.diag.message_primary or ''
                ambiguous=re.fullmatch(r'column reference "([a-z_]+)" is ambiguous',primary)
                if ambiguous:report['ambiguous_identifier']=ambiguous[1]
                break
            current=current.__context__
        report['frames']=frames
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
