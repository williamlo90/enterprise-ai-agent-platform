"""SPIKE-02 backend factory probe: Nova Pro APAC + Cohere Singapore, fictional only."""
import argparse
import os
import boto3
from botocore.config import Config
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from enterprise_ai_platform.composition import build_model_adapters
from enterprise_ai_platform.integrations.models.nova import NOVA_MODEL,NOVA_PROFILE,DESTINATIONS
from enterprise_ai_platform.modules.models.domain import EMBED_MODEL,REGION,DIMENSIONS,ModelFailure
from enterprise_ai_platform.modules.models.domain import Message,PolicyResult
from enterprise_ai_platform import model_check as legacy

FOLDER=Path('local/spike02/nova-integration')
PRICES={'chat_input':'1.08','chat_output':'4.32','embedding_input':'0.1'}
SOURCE_FILES=['backend/src/enterprise_ai_platform/'+p for p in (
    'nova_model_check.py','composition.py','model_check.py','integrations/models/bedrock.py','integrations/models/nova.py',
    'modules/models/domain.py','modules/models/ports.py','modules/models/__init__.py','integrations/models/__init__.py')]
CASES={
 'id':{'chat':'Kebijakan fiktif policy-v1: klaim biaya perjalanan wajib diajukan dalam 7 hari kalender setelah pulang. Ulangi aturan tenggat itu dalam satu kalimat bahasa Indonesia. Jangan menghitung tanggal.',
       'tool':'Berapa tenggat pengajuan klaim biaya perjalanan? Gunakan lookup_demo_policy untuk topic travel, language id sebelum menjawab. Ulangi aturan hasil tool dalam bahasa Indonesia tanpa menghitung tanggal.','word':'hari','claim_word':'klaim'},
 'en':{'chat':'Fictional policy-v1: travel expense claims must be submitted within 7 calendar days after returning. Repeat this deadline rule in one English sentence. Do not calculate dates.',
       'tool':'What is the travel expense claim submission deadline? Use lookup_demo_policy for topic travel, language en before answering. Repeat the tool policy rule in English without calculating dates.','word':'days','claim_word':'claim'},
 'documents':legacy.CASES['documents'],'queries':legacy.CASES['queries'],'retrieval':legacy.CASES['retrieval'],
 'tool_policy':'Travel expense claims must be submitted within 7 calendar days after returning from the trip. No leave entitlement or trip duration is defined.'
}


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def fixture_hash():return hashlib.sha256(legacy.encoded(CASES)).hexdigest()
def fingerprint():
    legacy.require(Path(__file__).resolve()==Path(SOURCE_FILES[0]).resolve())
    return {p:sha(Path(p)) for p in SOURCE_FILES}


def frozen_session():
    legacy.require(not os.environ.get('AWS_BEARER_TOKEN_BEDROCK'))
    credentials=boto3.Session(region_name=REGION).get_credentials()
    legacy.require(credentials is not None)
    frozen=credentials.get_frozen_credentials()
    return boto3.Session(region_name=REGION,aws_access_key_id=frozen.access_key,
                         aws_secret_access_key=frozen.secret_key,aws_session_token=frozen.token)


def availability(control,model,inference_type):
    detail=control.get_foundation_model(modelIdentifier=model)['modelDetails']
    legacy.require(detail['modelId']==model and detail['modelLifecycle']['status']=='ACTIVE' and inference_type in detail['inferenceTypesSupported'])
    value=control.get_foundation_model_availability(modelId=model)
    return {k:value.get(k) for k in ('authorizationStatus','entitlementAvailability','regionAvailability')}|{'agreement':value.get('agreementAvailability',{}).get('status')}


def preflight(session=None):
    original=json.loads(Path('local/spike02/setup.json').read_text(encoding='utf-8'))
    session=session or frozen_session()
    config=legacy.CONTROL_CONFIG.merge(Config(signature_version='v4'))
    control=session.client('bedrock',region_name=REGION,endpoint_url='https://bedrock.ap-southeast-1.amazonaws.com',config=config)
    identity=session.client('sts',region_name=REGION,endpoint_url='https://sts.ap-southeast-1.amazonaws.com',config=config)
    try:
        account=identity.get_caller_identity()['Account']
        legacy.require(original['region']==REGION and original['account']==account)
        profile=control.get_inference_profile(inferenceProfileIdentifier=NOVA_PROFILE)
        regions=sorted({row['modelArn'].split(':')[3] for row in profile['models']})
        ready={'profile_active':profile['status']=='ACTIVE' and regions==list(DESTINATIONS)}
        for label,model,inference_type in [('chat',NOVA_MODEL,'INFERENCE_PROFILE'),('embedding',EMBED_MODEL,'ON_DEMAND')]:
            state=availability(control,model,inference_type)
            ready[label]=all(state.get(k)==v for k,v in {'authorizationStatus':'AUTHORIZED','entitlementAvailability':'AVAILABLE','regionAvailability':'AVAILABLE','agreement':'AVAILABLE'}.items())
        ready['retention_none']=control.get_account_data_retention().get('mode')=='none'
        ready['raw_logging_disabled']=not bool(control.get_model_invocation_logging_configuration().get('loggingConfig'))
        _,embedding_prices=legacy.offer_prices(control,EMBED_MODEL)
        ready['embedding_price']=embedding_prices['embedding_input']==PRICES['embedding_input']
        pricing=Path('local/spike02/nova-comparison/pricing.json')
        card=json.loads(pricing.read_text(encoding='utf-8'))
        ready['nova_price']=(card['region']==REGION and card['tier']=='Standard' and card['unit']=='USD_per_million_tokens' and
                             card['prices']['pro']['input']==PRICES['chat_input'] and card['prices']['pro']['output']==PRICES['chat_output'])
        return {'account':account,'region':REGION,'chat_model':NOVA_MODEL,'chat_profile':NOVA_PROFILE,
                'embedding_model':EMBED_MODEL,'dimensions':DIMENSIONS,'prices':PRICES,'destination_regions':regions,
                'source_sha256':fingerprint(),'fixture_sha256':fixture_hash(),'lock_sha256':sha(Path('backend/uv.lock')),
                'pricing_sha256':sha(pricing),'original_setup_sha256':sha(Path('local/spike02/setup.json'))},ready
    finally:control.close();identity.close()


def prepare():
    manifest,ready=preflight();legacy.require(all(ready.values()))
    FOLDER.mkdir(parents=True,exist_ok=True)
    legacy.exclusive_claim(FOLDER/'manifest.json',manifest)
    print(json.dumps({'prepared':True,'read_only':True,'paid_calls':0,'checks':ready}))


def exercise(adapters,proof):
    proof.data['fictional_chat_outputs']=[]
    for language in ('id','en'):
        case=CASES[language]
        answer=proof.perform(adapters.chat,lambda:adapters.chat.chat([Message('user',text=case['chat'])]))
        proof.check('chat_'+language,answer.tool is None and '7' in answer.text and case['word'] in answer.text.lower() and case['claim_word'] in answer.text.lower())
        proof.data['fictional_chat_outputs'].append({'case':'chat_'+language,'text':answer.text});proof.save()
        messages=[Message('user',text=case['tool'])]
        selected=proof.perform(adapters.chat,lambda:adapters.chat.chat(messages,tools=True))
        proof.check('tool_schema_'+language,selected.tool is not None and selected.tool.language==language)
        messages.extend([selected.message,Message('user',result=PolicyResult(selected.tool.id,7,'policy-v1',CASES['tool_policy']))])
        final=proof.perform(adapters.chat,lambda:adapters.chat.chat(messages,tools=True))
        proof.check('tool_roundtrip_'+language,final.tool is None and '7' in final.text and case['word'] in final.text.lower() and case['claim_word'] in final.text.lower())
        proof.data['fictional_chat_outputs'].append({'case':'tool_roundtrip_'+language,'text':final.text});proof.save()
    documents=proof.perform(adapters.embedding,lambda:adapters.embedding.embed(CASES['documents'],input_type='search_document'))
    queries=proof.perform(adapters.embedding,lambda:adapters.embedding.embed(CASES['queries'],input_type='search_query'))
    for index,(left,right,winner) in enumerate(CASES['retrieval']):
        scores={n:legacy.cosine(queries.vectors[index],documents.vectors[n]) for n in (left,right)}
        proof.check('cross_language_retrieval_'+str(index+1),scores[winner]>scores[right if winner==left else left])
    proof.check('embedding_shape_and_finite_vectors',len(documents.vectors)==len(queries.vectors)==4)


def worker(path):
    manifest=json.loads((FOLDER/'manifest.json').read_text(encoding='utf-8'))
    claim=json.loads((FOLDER/'run-claim.json').read_text(encoding='utf-8'))
    legacy.require(path.name=='proof.json' and path.resolve().parent.parent==(FOLDER/'runs').resolve() and path.parent.name==claim['run_id'])
    legacy.require(claim['manifest_sha256']==sha(FOLDER/'manifest.json'))
    legacy.exclusive_claim(path.with_name('worker-claim.json'),{'claimed':True})
    data=json.loads(path.read_text(encoding='utf-8'));proof=legacy.Proof(path,data,prices=PRICES)
    try:
        legacy.require(data.get('status')=='running' and data.get('attempts')==[] and data.get('checks')==[])
        legacy.require(data.get('manifest_sha256')==claim['manifest_sha256'])
        legacy.require(data.get('prices_usd_per_million')==PRICES and data.get('max_paid_attempts')==8 and data.get('reservation_limit_usd')=='0.10')
        legacy.require(data.get('chat_profile')==NOVA_PROFILE and data.get('embedding_model')==EMBED_MODEL)
        legacy.require(data.get('scope')=='fictional_APAC_chat_Singapore_embedding_not_product_acceptance' and data.get('automatic_retry') is False)
        session=frozen_session()
        observed,ready=preflight(session);legacy.require(observed==manifest and all(ready.values()))
        legacy.require(data['source_sha256']==fingerprint() and data['fixture_sha256']==fixture_hash())
        data['preflight']=ready;proof.save()
        with build_model_adapters(proof.reserve,fictional_apac=True,session=session) as adapters:exercise(adapters,proof)
        legacy.require(data['source_sha256']==fingerprint())
        data.update(passed=True,status='passed',check_count=len(data['checks']))
    except Exception as error:
        data.update(passed=False,status='failed',code=error.code if isinstance(error,ModelFailure) else 'sanitized_probe_failure',check_count=len(data['checks']))
        if isinstance(error,ModelFailure):data['failure']={'code':error.code,'operation':error.operation,'provider_code':error.provider_code}
    finally:
        data['paid_attempt_count']=len(data['attempts']);data['finished_at']=datetime.now(timezone.utc).isoformat()
        data['known_token_cost_usd']=str(sum(Decimal(a['actual_token_cost_usd']) for a in data['attempts'] if a.get('actual_token_cost_usd') is not None))
        data['attempts_without_exact_usage']=sum(a.get('usage') is None for a in data['attempts']);data['final_billed_cost_verified']=False
        proof.save()


def run():
    manifest=json.loads((FOLDER/'manifest.json').read_text(encoding='utf-8'))
    observed,ready=preflight();legacy.require(observed==manifest and all(ready.values()))
    run_id=uuid4().hex
    legacy.exclusive_claim(FOLDER/'run-claim.json',{'run_id':run_id,'manifest_sha256':sha(FOLDER/'manifest.json')})
    directory=FOLDER/'runs'/run_id;directory.mkdir(parents=True);path=directory/'proof.json'
    data={'passed':False,'status':'running','started_at':datetime.now(timezone.utc).isoformat(),'source_sha256':fingerprint(),'fixture_sha256':fixture_hash(),
          'manifest_sha256':sha(FOLDER/'manifest.json'),'scope':'fictional_APAC_chat_Singapore_embedding_not_product_acceptance',
          'chat_profile':NOVA_PROFILE,'embedding_model':EMBED_MODEL,'prices_usd_per_million':PRICES,
          'checks':[],'attempts':[],'max_paid_attempts':8,'reservation_limit_usd':'0.10','automatic_retry':False}
    legacy.Proof(path,data,prices=PRICES).save()
    try:
        completed=subprocess.run([sys.executable,'-m','enterprise_ai_platform.nova_model_check','worker','--live','--proof',str(path)],timeout=150,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        result=json.loads(path.read_text(encoding='utf-8'))
        if completed.returncode or result.get('status')=='running':result.update(passed=False,status='failed',code='worker_failed')
    except subprocess.TimeoutExpired:
        result=json.loads(path.read_text(encoding='utf-8'));result.update(passed=False,status='failed',code='deadline',remote_request_may_continue=True)
    for attempt in result['attempts']:
        if attempt['state']=='pending':attempt['state']='unknown_remote_outcome'
    legacy.Proof(path,result,prices=PRICES).save()
    print(json.dumps({'passed':result['passed'],'code':result.get('code'),'checks':len(result['checks']),'paid_attempts':len(result['attempts']),'proof':str(path)}))
    if not result['passed']:raise SystemExit(1)


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['prepare','preflight','run','worker']);parser.add_argument('--live',action='store_true');parser.add_argument('--proof',type=Path);args=parser.parse_args()
    try:
        if args.command=='prepare':prepare()
        elif args.command=='preflight':
            _,ready=preflight();print(json.dumps({'passed':all(ready.values()),'read_only':True,'checks':ready}))
            if not all(ready.values()):raise SystemExit(1)
        else:
            legacy.require(args.live)
            if args.command=='run':run()
            else:legacy.require(args.proof is not None);worker(args.proof)
    except Exception as error:
        print(json.dumps({'passed':False,'code':error.code if isinstance(error,ModelFailure) else 'sanitized_probe_failure','automatic_retry':False}));raise SystemExit(1) from None


if __name__=='__main__':main()
