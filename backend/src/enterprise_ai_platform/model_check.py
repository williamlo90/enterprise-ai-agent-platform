"""SPIKE-02: prepare/read-only preflight and explicit fictional-only model probe.

No activation, subscription, IAM, retention or logging mutation is implemented.
"""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import logging
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from uuid import uuid4
import boto3
import botocore
from botocore.config import Config
from enterprise_ai_platform.integrations.models.bedrock import BedrockModels,SDK_CONFIG,ENDPOINT
from enterprise_ai_platform.modules.models.domain import CHAT_MODEL,EMBED_MODEL,REGION,DIMENSIONS,MAX_INPUT_TOKENS,MAX_OUTPUT_TOKENS,TOOL_NAME,ModelFailure

FOLDER=Path('local/spike02')
CONTROL_CONFIG=Config(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1})
PRICES={'chat_input':'2.2','chat_output':'11','embedding_input':'0.1'}
SOURCE_FILES=['backend/src/enterprise_ai_platform/'+p for p in ('model_check.py','integrations/models/bedrock.py','modules/models/domain.py','modules/models/ports.py','modules/models/__init__.py','integrations/models/__init__.py')]
CASES={
 'id':{'chat':'Kebijakan fiktif: klaim perjalanan harus diajukan dalam 7 hari. Berapa batas waktunya? Jawab satu kalimat dalam bahasa Indonesia dengan angka 7 dan kata hari.',
       'tool':'Gunakan lookup_demo_policy untuk topic travel, language id, lalu jelaskan batas pengajuan klaim dalam bahasa Indonesia. Jangan menjawab sebelum menerima hasil tool.','word':'hari'},
 'en':{'chat':'Fictional policy: travel claims must be submitted within 7 days. What is the deadline? Answer in one English sentence using the number 7 and the word days.',
       'tool':'Use lookup_demo_policy with topic travel and language en, then explain the claim deadline in English. Do not answer before receiving the tool result.','word':'days'},
 'documents':['Travel reimbursement: employees submit business trip expense claims within seven days of returning.',
              'Annual leave: employees request vacation days from their manager before taking time off.',
              'Penggantian biaya perjalanan: karyawan mengajukan klaim dinas paling lambat tujuh hari setelah pulang.',
              'Cuti tahunan: karyawan meminta izin liburan kepada manajer sebelum mengambil cuti.'],
 'queries':['Berapa lama batas mengajukan biaya perjalanan dinas?','How do I ask for vacation leave?',
            'What is the deadline for a business travel expense claim?','Bagaimana cara meminta cuti liburan?'],
 'retrieval':[[0,1,0],[2,3,3],[2,3,2],[0,1,1]],
}


def require(value):
    if not value:raise ModelFailure('preflight_failed')


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
def fingerprint():
    # The probe is repo-run: never label a stale installed wheel with repo hashes.
    require(Path(__file__).resolve()==Path(SOURCE_FILES[0]).resolve())
    return {p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in SOURCE_FILES}
def fixture_hash():return hashlib.sha256(encoded(CASES)).hexdigest()


def clients():
    session=boto3.Session(region_name=REGION)
    return (session.client('bedrock',endpoint_url='https://bedrock.ap-southeast-1.amazonaws.com',config=CONTROL_CONFIG),
            session.client('sts',config=CONTROL_CONFIG))


def offer_prices(control,model_id):
    response=control.list_foundation_model_agreement_offers(modelId=model_id,offerType='PUBLIC')
    offers=response.get('offers',[]);require(len(offers)==1)
    offer=offers[0];rates=offer.get('termDetails',{}).get('usageBasedPricingTerm',{}).get('rateCard',[])
    names={'APS1_input_tokens_standard':'chat_input','APS1_output_tokens_standard':'chat_output'} if model_id==CHAT_MODEL else {'APS1_InputTokenCount':'embedding_input'}
    values={}
    for row in rates:
        if row.get('dimension') in names:
            name=names[row['dimension']];require(name not in values)
            values[name]=str(Decimal(row['price']).normalize())
    require(set(values)==set(names.values()) and all(Decimal(v)==Decimal(PRICES[k]) for k,v in values.items()))
    return offer,values


def availability(control,model_id):
    detail=control.get_foundation_model(modelIdentifier=model_id)['modelDetails']
    require(detail['modelId']==model_id and detail['modelLifecycle']['status']=='ACTIVE' and 'ON_DEMAND' in detail['inferenceTypesSupported'])
    value=control.get_foundation_model_availability(modelId=model_id)
    return {k:value.get(k) for k in ('authorizationStatus','entitlementAvailability','regionAvailability')}|{'agreement':value.get('agreementAvailability',{}).get('status')}


def prepare():
    control,sts=clients();account=sts.get_caller_identity()['Account']
    # Existing setup is immutable; operators inspect it instead of silently rebind.
    FOLDER.mkdir(parents=True,exist_ok=True);require(not (FOLDER/'setup.json').exists())
    setup=dict(region=REGION,account=account,chat_model=CHAT_MODEL,embedding_model=EMBED_MODEL,dimensions=DIMENSIONS,
               prices=PRICES,budget_target_usd='1',probe_reservation_limit_usd='0.10',max_paid_attempts=8,created_at=datetime.now(timezone.utc).isoformat())
    observed={}
    for name,mid in [('chat',CHAT_MODEL),('embedding',EMBED_MODEL)]:
        offer,_=offer_prices(control,mid);observed[name]=availability(control,mid)
        path=FOLDER/('activate-'+name+'.json')
        with path.open('x',encoding='utf-8') as stream:json.dump({'modelId':mid,'offerToken':offer['offerToken']},stream)
        # Terms stay private/local for operator review; never print offer tokens.
        with (FOLDER/('terms-'+name+'.json')).open('x',encoding='utf-8') as stream:json.dump(offer.get('termDetails',{}),stream,indent=2)
    with (FOLDER/'retention-none.json').open('x',encoding='utf-8') as stream:json.dump({'mode':'none'},stream)
    with (FOLDER/'setup.json').open('x',encoding='utf-8') as stream:json.dump(setup,stream,indent=2)
    print(json.dumps(dict(prepared=True,aws_mutated=False,region=REGION,model_access=observed)))


def preflight():
    setup=json.loads((FOLDER/'setup.json').read_text(encoding='utf-8'))
    require(setup['region']==REGION and setup['chat_model']==CHAT_MODEL and setup['embedding_model']==EMBED_MODEL and setup['dimensions']==DIMENSIONS)
    require(setup['prices']==PRICES and setup['budget_target_usd']=='1' and setup['probe_reservation_limit_usd']=='0.10' and setup['max_paid_attempts']==8)
    control,sts=clients();require(sts.get_caller_identity()['Account']==setup['account'])
    readiness={}
    for name,mid in [('chat',CHAT_MODEL),('embedding',EMBED_MODEL)]:
        state=availability(control,mid);offer_prices(control,mid)
        readiness[name]=all(state.get(k)==v for k,v in {'authorizationStatus':'AUTHORIZED','entitlementAvailability':'AVAILABLE','regionAvailability':'AVAILABLE','agreement':'AVAILABLE'}.items())
    readiness['anthropic_form_present']=bool(control.get_use_case_for_model_access().get('formData'))
    readiness['retention_none']=control.get_account_data_retention().get('mode')=='none'
    readiness['raw_logging_disabled']=not bool(control.get_model_invocation_logging_configuration().get('loggingConfig'))
    return setup,readiness


class Proof:
    def __init__(self,path,data,*,prices=None):self.path,self.data=path,data;self.prices=PRICES if prices is None else prices;self.start=time.monotonic()
    def save(self):
        temporary=self.path.with_name('proof-'+uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(self.data,indent=2)+'\n',encoding='utf-8');temporary.replace(self.path)
    def reserve(self,kind,input_tokens):
        if time.monotonic()-self.start>120:raise ModelFailure('deadline')
        attempts=self.data['attempts']
        if kind not in ('chat','embedding') or len(attempts)>=8 or sum(a['kind']==kind for a in attempts)>=(6 if kind=='chat' else 2):raise ModelFailure('budget_exhausted')
        tokens=MAX_INPUT_TOKENS if kind=='chat' else input_tokens
        reservation=(Decimal(tokens)*Decimal(self.prices['chat_input' if kind=='chat' else 'embedding_input'])+(Decimal(MAX_OUTPUT_TOKENS)*Decimal(self.prices['chat_output']) if kind=='chat' else Decimal(0)))/Decimal(1000000)
        total=sum(Decimal(a['reserved_usd']) for a in attempts)
        if total+reservation>Decimal('0.10'):raise ModelFailure('budget_exhausted')
        attempts.append(dict(kind=kind,state='pending',reserved_usd=str(reservation),usage=None,actual_token_cost_usd=None))
        self.save()  # Must succeed before remote inference starts.
    def settle(self,usage,success):
        attempt=self.data['attempts'][-1];attempt['state']='completed' if success else 'failed_usage_unknown' if usage is None else 'failed'
        if usage is not None:
            attempt['usage']=dict(input_tokens=usage.input_tokens,output_tokens=usage.output_tokens)
            price=Decimal(self.prices['chat_input' if attempt['kind']=='chat' else 'embedding_input'])
            attempt['actual_token_cost_usd']=str((Decimal(usage.input_tokens)*price+Decimal(usage.output_tokens)*Decimal(self.prices['chat_output']))/Decimal(1000000))
        self.save()
    def perform(self,adapter,call):
        before=len(self.data['attempts']);start=time.monotonic();success=False
        try:
            value=call();success=True;return value
        except ModelFailure as error:
            if len(self.data['attempts'])>before:
                self.data['attempts'][-1]['failure']=dict(code=error.code,operation=error.operation,provider_code=error.provider_code)
            raise
        finally:
            if len(self.data['attempts'])>before:
                self.data['attempts'][-1]['elapsed_seconds']=round(time.monotonic()-start,3)
                self.settle(adapter.last_usage,success)
    def check(self,name,value):
        if not value:raise ModelFailure('invalid_output')
        self.data['checks'].append(name);self.save()


def cosine(a,b):return sum(x*y for x,y in zip(a,b))/math.sqrt(sum(x*x for x in a)*sum(y*y for y in b))


def exercise(adapter,proof):
    for language in ('id','en'):
        case=CASES[language]
        response=proof.perform(adapter,lambda:adapter.chat([{'role':'user','content':[{'text':case['chat']}]}]))
        proof.check('chat_'+language,response.tool is None and '7' in response.text and case['word'] in response.text.lower())
        messages=[{'role':'user','content':[{'text':case['tool']}]}]
        tool=proof.perform(adapter,lambda:adapter.chat(messages,tools=True))
        proof.check('tool_schema_'+language,tool.tool is not None and tool.tool.language==language)
        # A server-owned fictional read only; no tenant, session, URL or action.
        messages.extend([tool.message,{'role':'user','content':[{'toolResult':{'toolUseId':tool.tool.id,'content':[{'json':{'fictional':True,'submission_deadline_days':7}}],'status':'success'}}]}])
        final=proof.perform(adapter,lambda:adapter.chat(messages,tools=True))
        proof.check('tool_roundtrip_'+language,final.tool is None and '7' in final.text and case['word'] in final.text.lower())
    documents=proof.perform(adapter,lambda:adapter.embed(CASES['documents'],input_type='search_document'))
    queries=proof.perform(adapter,lambda:adapter.embed(CASES['queries'],input_type='search_query'))
    for index,(left,right,winner) in enumerate(CASES['retrieval']):
        scores={n:cosine(queries.vectors[index],documents.vectors[n]) for n in (left,right)}
        proof.check('cross_language_retrieval_'+str(index+1),scores[winner]>scores[right if winner==left else left])
    proof.check('embedding_shape_and_finite_vectors',len(documents.vectors)==len(queries.vectors)==4)


def exclusive_claim(path,value):
    try:
        with path.open('x',encoding='utf-8') as stream:json.dump(value,stream)
    except FileExistsError:raise ModelFailure('preflight_failed') from None


def validate_retry(path):
    path=path.resolve()
    require(path.parent==(FOLDER/'retry-authorizations').resolve())
    authorization=json.loads(path.read_text(encoding='utf-8'))
    prior=authorization.get('prior_run_id','');new=authorization.get('new_run_id','')
    require(re.fullmatch('[a-f0-9]{32}',prior) is not None and re.fullmatch('[a-f0-9]{32}',new) is not None and prior!=new)
    prior_path=FOLDER/'runs'/prior/'proof.json'
    require(hashlib.sha256(prior_path.read_bytes()).hexdigest()==authorization['prior_proof_sha256'])
    previous=json.loads(prior_path.read_text(encoding='utf-8'))
    require(previous.get('status')=='failed' and previous.get('code')=='invalid_input' and previous.get('paid_attempt_count')==0 and previous.get('attempts')==[])
    require(hashlib.sha256((FOLDER/'run-claim.json').read_bytes()).hexdigest()==authorization['prior_claim_sha256'])
    require(json.loads((FOLDER/'run-claim.json').read_text(encoding='utf-8'))['run_id']==prior)
    require(authorization['source_sha256']==fingerprint() and authorization['fixture_sha256']==fixture_hash())
    require(authorization['lock_sha256']==hashlib.sha256(Path('backend/uv.lock').read_bytes()).hexdigest())
    require(authorization['setup_sha256']==hashlib.sha256((FOLDER/'setup.json').read_bytes()).hexdigest())
    require(authorization['region']==REGION and authorization['chat_model']==CHAT_MODEL and authorization['embedding_model']==EMBED_MODEL)
    require(authorization['max_paid_attempts']==8 and authorization['reservation_limit_usd']=='0.10' and authorization['maximum_uses']==1)
    return authorization


def worker(path):
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('retry_authorization'):
        auth_path=Path(data['retry_authorization']);authorization=validate_retry(auth_path)
        require(hashlib.sha256(auth_path.read_bytes()).hexdigest()==data['retry_authorization_sha256'])
        require(path.parent.name==authorization['new_run_id'])
        consumed=json.loads((FOLDER/'retry-claims'/(authorization['prior_run_id']+'.json')).read_text(encoding='utf-8'))
        require(consumed['authorization_sha256']==data['retry_authorization_sha256'] and consumed['new_run_id']==path.parent.name)
    else:
        claim=json.loads((FOLDER/'run-claim.json').read_text(encoding='utf-8'))
        require(path.parent.name==claim['run_id'])
    require(path.name=='proof.json' and path.resolve().parent.parent==(FOLDER/'runs').resolve())
    exclusive_claim(path.with_name('worker-claim.json'),dict(claimed=True))
    data=json.loads(path.read_text(encoding='utf-8'));proof=Proof(path,data)
    require(data['source_sha256']==fingerprint() and data['fixture_sha256']==fixture_hash() and data['status']=='running')
    try:
        _,ready=preflight();data['preflight']=ready;proof.save();require(all(ready.values()))
        runtime=boto3.client('bedrock-runtime',region_name=REGION,endpoint_url=ENDPOINT,config=SDK_CONFIG)
        adapter=BedrockModels(runtime,proof.reserve)
        try:exercise(adapter,proof)
        finally:runtime.close()
        require(data['source_sha256']==fingerprint())
        data.update(passed=True,status='passed',check_count=len(data['checks']))
    except Exception as error:
        data.update(passed=False,status='failed',code=error.code if isinstance(error,ModelFailure) else 'sanitized_probe_failure',check_count=len(data['checks']))
        if isinstance(error,ModelFailure):data['failure']=dict(code=error.code,operation=error.operation,provider_code=error.provider_code)
    finally:
        data['paid_attempt_count']=len(data['attempts']);data['finished_at']=datetime.now(timezone.utc).isoformat()
        data['known_token_cost_usd']=str(sum(Decimal(a['actual_token_cost_usd']) for a in data['attempts'] if a['actual_token_cost_usd'] is not None))
        data['attempts_without_exact_usage']=sum(a['usage'] is None for a in data['attempts'])
        data['final_billed_cost_verified']=False;proof.save()


def run(retry_authorization=None):
    root=FOLDER/'runs';root.mkdir(parents=True,exist_ok=True)
    authorization=None
    if retry_authorization is None:
        run_id=uuid4().hex
        exclusive_claim(FOLDER/'run-claim.json',dict(run_id=run_id,created_at=datetime.now(timezone.utc).isoformat()))
        require(not any(root.glob('*/proof.json')))
    else:
        authorization=validate_retry(retry_authorization);run_id=authorization['new_run_id']
        # One reviewed recovery of a zero-inference failure. Existing evidence
        # and the initial claim remain unchanged; consume authorization atomically.
        require({p.parent.name for p in root.glob('*/proof.json')}=={authorization['prior_run_id']})
        claims=FOLDER/'retry-claims';claims.mkdir(parents=True,exist_ok=True)
        exclusive_claim(claims/(authorization['prior_run_id']+'.json'),dict(new_run_id=run_id,authorization_sha256=hashlib.sha256(retry_authorization.read_bytes()).hexdigest()))
    _,ready=preflight();require(all(ready.values()))
    directory=root/run_id;directory.mkdir();path=directory/'proof.json'
    initial=dict(passed=False,status='running',region=REGION,chat_model=CHAT_MODEL,embedding_model=EMBED_MODEL,dimensions=DIMENSIONS,
                 source_sha256=fingerprint(),fixture_sha256=fixture_hash(),setup_sha256=hashlib.sha256((FOLDER/'setup.json').read_bytes()).hexdigest(),lock_sha256=hashlib.sha256(Path('backend/uv.lock').read_bytes()).hexdigest(),
                 python=platform.python_version(),boto3=boto3.__version__,botocore=botocore.__version__,prices_usd_per_million=PRICES,
                 data_category='fictional_only',checks=[],attempts=[],max_paid_attempts=8,reservation_limit_usd='0.10',budget_target_usd='1',
                 started_at=datetime.now(timezone.utc).isoformat(),cloud_resources_created=False,do_not_retry_automatically=True,
                 input_budget_method='serialized_request_utf8_bytes_3072_conservative_reservation_not_exact_token_count')
    if authorization is not None:
        initial.update(retry_authorization=str(retry_authorization.resolve()),retry_authorization_sha256=hashlib.sha256(retry_authorization.read_bytes()).hexdigest(),
                       retry_of_run_id=authorization['prior_run_id'],prior_proof_sha256=authorization['prior_proof_sha256'])
    with path.open('x',encoding='utf-8') as stream:json.dump(initial,stream,indent=2)
    try:
        completed=subprocess.run([sys.executable,'-m','enterprise_ai_platform.model_check','worker','--live','--proof',str(path)],timeout=150,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        result=json.loads(path.read_text(encoding='utf-8'))
        if completed.returncode or result.get('status')=='running':
            for attempt in result['attempts']:
                if attempt['state']=='pending':attempt['state']='unknown_remote_outcome'
            result.update(passed=False,status='failed',code='worker_failed');Proof(path,result).save()
    except subprocess.TimeoutExpired:
        result=json.loads(path.read_text(encoding='utf-8'))
        for attempt in result['attempts']:
            if attempt['state']=='pending':attempt['state']='unknown_remote_outcome'
        result.update(passed=False,status='failed',code='deadline',remote_request_may_continue=True);Proof(path,result).save()
    print(json.dumps(dict(passed=result['passed'],code=result.get('code'),check_count=len(result['checks']),paid_attempt_count=len(result['attempts']),proof=str(path))))
    if not result['passed']:raise SystemExit(1)


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['prepare','preflight','run','worker']);parser.add_argument('--live',action='store_true');parser.add_argument('--proof',type=Path);parser.add_argument('--retry-authorization',type=Path);args=parser.parse_args()
    try:
        if args.command=='prepare':prepare()
        elif args.command=='preflight':
            _,ready=preflight();print(json.dumps(dict(passed=all(ready.values()),read_only=True,checks=ready)))
            if not all(ready.values()):raise SystemExit(1)
        else:
            require(args.live)
            if args.command=='worker':
                require(args.proof is not None and args.proof.resolve().is_relative_to((FOLDER/'runs').resolve()))
                worker(args.proof)
            else:run(args.retry_authorization)
    except Exception as error:
        print(json.dumps(dict(passed=False,code=error.code if isinstance(error,ModelFailure) else 'sanitized_probe_failure')));raise SystemExit(1) from None


if __name__=='__main__':main()
