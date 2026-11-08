"""Actual private receipt API/root/service/crypto/SQL in an owned schema."""
import argparse
import asyncio
import copy
import hashlib
import json
import logging
from pathlib import Path
import secrets
import sys
import time
import traceback
from uuid import uuid4
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
from psycopg.types.json import Jsonb
from check_receipt_registry import Harness as RegistryHarness
from check_knowledge_workspace import verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,receipt_service,database_loop_factory
from enterprise_ai_platform.config import Settings,load_settings,DatabaseProbeSettings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.integrations.security.receipt_terminal import ReceiptTerminalVerifier,TerminalKey
from enterprise_ai_platform.integrations.security.receipt_settlement import ReceiptSettlement,SettlementKey
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.receipts.domain import PinBatch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'reference_service/src'))
from reimbursement_service.integrations.security.terminal import Signer
from reimbursement_service.modules.operations.domain import Identity,Terminal,Result


class LostReply(httpx.AsyncBaseTransport):
    def __init__(self,app):self.inner=httpx.ASGITransport(app=app)
    async def handle_async_request(self,request):
        response=await self.inner.handle_async_request(request)
        await response.aclose()
        raise httpx.ReadTimeout('Controlled lost response.')
    async def aclose(self):await self.inner.aclose()


class Harness(RegistryHarness):
    async def api_receipts(self):
        await self.registry()
        moment=int(time.time());root=secrets.token_bytes(32);private=Ed25519PrivateKey.generate()
        await self.execute("INSERT INTO {s}.receipt_receiver_keys VALUES(%s,'api',%s,true,clock_timestamp()-interval '10 seconds',clock_timestamp()+interval '1 hour')",(self.credential_hash,root))
        signer=Signer(kid='api',private_bytes=private.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption()))
        verifier=ReceiptTerminalVerifier({'api':TerminalKey(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),moment-60,moment+3600)})
        services=ApplicationServices(health=None,receipts=receipt_service(self.database,self.delegation,proofs=verifier,
            settlement=ReceiptSettlement(SettlementKey('api',root,moment-60,moment+3600))))
        settings=Settings(env='test',host='127.0.0.1',port=8000)
        app=create_internal_app(settings,services=services);public=create_app(settings,services=services)
        async def prepare(client):
            uploaded=await self.upload(b'%PDF-1.4\nFictional private API receipt',purpose='receipt',mime='application/pdf')
            require(uploaded.status_code==202,'api-receipt-upload');await self.process()
            file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
            payload=self.submission(self.people[0]);payload['data']['receipts']=[dict(id=file['id'],version=file['version'],digest=file['digest'])]
            payload['data']['items']=[dict(id=str(uuid4()),trip_item_id=str(uuid4()),amount_minor=100,receipt_ids=[file['id']])];payload['data']['total_minor']=100
            token,claims=await self.authority(self.people[0],payload)
            await self.execute('INSERT INTO {s}.receipt_human_fixture(id,claims) VALUES(%s,%s)',(claims['human_receipt_id'],Jsonb(claims)))
            body=dict(payload=payload,key=str(uuid4()),candidate_claim_id=str(uuid4()))
            response=await client.post('/internal/v1/receipt-pins',json=body,headers={'authorization':'Bearer '+self.credential,'x-eai-delegation':token,'content-type':'application/json'})
            require(response.status_code==200,'actual-api-prepare-'+str(response.status_code))
            require(response.headers.get('cache-control')=='no-store' and response.json()['pins'][0]['candidate_claim_id']==body['candidate_claim_id'],'api-candidate-private')
            row=await services.receipts.repository.lookup(self.credential_hash,payload['tenant_id'],payload['operation_id'])
            return PinBatch.from_wire(row['batch']),response
        terminals={}
        def terminal(batch,*,signer_override=None,ref=None):
            now=int(time.time());cache_key=(batch.identity.operation_id,ref or batch.claim_id)
            if cache_key not in terminals:terminals[cache_key]=Terminal(Identity(**batch.identity.wire()),'committed',Result(ref or batch.claim_id,1,'claim_submitted'),now)
            value=terminals[cache_key]
            return (signer_override or signer).issue(value,authority_deadline=now+40)
        headers={'authorization':'Bearer '+self.credential,'content-type':'application/json'}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://receipt-api.test') as client:
                batch,response=await prepare(client)
                self.passed('actual_internal_factory_API_prepares_candidate_bound_private_pin_using_live_restricted_SQL_and_original_human_reference')
                path='/internal/v1/receipt-pins/'+batch.identity.operation_id+'/finalise'
                body=dict(tenant_id=batch.identity.tenant_id,proof=terminal(batch))
                wrong=dict(tenant_id=batch.identity.tenant_id,proof=terminal(batch,ref=str(uuid4())))
                require((await client.post(path,json=wrong,headers=headers)).status_code==403,'signed-wrong-candidate-denied')
                forged=copy.deepcopy(body);forged['proof']['signature']='A'*86
                require((await client.post(path,json=forged,headers=headers)).status_code==403,'api-forged-proof-denied')
                await self.execute('UPDATE {s}.internal_caller_credentials SET active=false WHERE credential_hash=%s',(self.credential_hash,))
                require((await client.post(path,json=body,headers=headers)).status_code==401,'api-current-revoked-caller')
                await self.execute('UPDATE {s}.internal_caller_credentials SET active=true WHERE credential_hash=%s',(self.credential_hash,))
                require((await services.receipts.repository.lookup(self.credential_hash,batch.identity.tenant_id,batch.identity.operation_id))['state']=='held','api-negatives-preserve-held')
                self.passed('actual_API_rejects_forged_signed_wrong_candidate_and_current_revoked_caller_without_releasing_held_pin')
                done=await client.post(path,json=body,headers=headers)
                require(done.status_code==200 and done.json()['state']=='finalised' and done.json()['claim_version']==1,'actual-api-finalise')
                await asyncio.sleep(max(0,body['proof']['payload']['issued_at']+1-time.time())+.05)
                renewed=terminal(batch)
                require(renewed['payload']['operation']==body['proof']['payload']['operation'],'reissue-same-immutable-terminal')
                require(renewed['payload']['issued_at']>body['proof']['payload']['issued_at'] and renewed['signature']!=body['proof']['signature'],'reissue-changed-envelope')
                again=await client.post(path,json=dict(tenant_id=batch.identity.tenant_id,proof=renewed),headers=headers)
                require(again.status_code==200 and again.json()['terminal_digest']==done.json()['terminal_digest'],'actual-api-terminal-reissue-replay')
                self.passed('actual_API_lookup_signature_verifier_private_MAC_and_SQL_finalise_chain_commits_and_accepts_exact_terminal_reissue')
                lost,_=await prepare(client);lost_path='/internal/v1/receipt-pins/'+lost.identity.operation_id+'/finalise'
                lost_body=dict(tenant_id=lost.identity.tenant_id,proof=terminal(lost))
                async with httpx.AsyncClient(transport=LostReply(app),base_url='https://receipt-api.test') as disconnected:
                    try:await disconnected.post(lost_path,json=lost_body,headers=headers)
                    except httpx.ReadTimeout:pass
                    else:raise CheckFailure('controlled-lost-finalise-response-not-observed')
                require((await services.receipts.repository.lookup(self.credential_hash,lost.identity.tenant_id,lost.identity.operation_id))['state']=='finalised','lost-finalise-already-committed')
                retry=await client.post(lost_path,json=lost_body,headers=headers)
                require(retry.status_code==200 and retry.json()['state']=='finalised','lost-finalise-retry-safe')
                self.passed('actual_post_commit_API_response_loss_is_recovered_by_exact_retry_without_releasing_or_rebinding_pin')
                before=await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')
                for url,h in [(path+'?role=admin',headers),(path,{**headers,'cookie':'untrusted=yes'}),(path,{**headers,'x-eai-delegation':'unexpected'})]:
                    denied=await client.post(url,json=body,headers=h);require(denied.status_code in {400,401},'private-api-input-denied')
                require(await self.scalar('SELECT count(*) FROM {s}.receipt_pin_batches')==before,'private-api-invalid-input-no-new-batch')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public),base_url='https://public-receipt.test') as browser:
                    require((await browser.post(path,json=body,headers=headers)).status_code==404,'public-finalise-absent')
                    require((await browser.post('/internal/v1/receipt-pins',json={},headers=headers)).status_code==404,'public-prepare-absent')
                self.passed('private_receipt_routes_reject_query_cookie_and_unexpected_delegation_and_remain_absent_on_public_factory')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned database fixture.')
    folder=Path('local/file02');folder.mkdir(parents=True,exist_ok=True);report={'passed':False}
    try:
        paths=[Path(__file__),Path('scripts/checks/check_receipt_registry.py'),Path('scripts/checks/check_policy_eligibility.py'),Path('scripts/checks/check_policy_draft.py'),Path('scripts/checks/check_knowledge_workspace.py'),
            Path('reference_service/src/reimbursement_service/integrations/security/terminal.py'),Path('reference_service/src/reimbursement_service/modules/operations/domain.py')]
        hashes={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=22,operation='api_receipts',direct_timeout=300))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'receipt-api-helpers-stable')
        report.update(helper_sha256=hashes,scope='actual internal receipt API/factory/service/crypto/SQL; controlled ACT/storage/parser and signed terminal fixture, no claim-ledger producer or download acceptance')
    except Exception as error:
        report.update(error_type=type(error).__name__)
        if isinstance(error,CheckFailure):report['check']=str(error)
        report['frames']=[dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)]
    (folder/'api-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'source_sha256','helper_sha256'}}))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':main()
