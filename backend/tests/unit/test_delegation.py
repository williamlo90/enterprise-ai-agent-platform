import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
from functools import wraps
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import httpx
import jwt
import pytest
from enterprise_ai_platform.delegation_check import SigningFixture,Receipts,ISSUER,AUDIENCE,CALLER
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier,RSADelegationSigner
from enterprise_ai_platform.modules.delegation.domain import canonical_payload,request_hash,validate_claims,HumanReceipt
from enterprise_ai_platform.modules.delegation.service import DelegationService
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure

FIXTURE=json.loads((Path(__file__).parents[1]/"contracts/delegation_cases.json").read_text(encoding="utf-8"))
NOW=1800000000


def add_receipt(reader,claims):
    receipt=HumanReceipt(claims['human_receipt_id'],claims['sub'],claims['session_id'],claims['tenant_id'],claims['operation_id'],claims['action'],claims['object_kind'],claims['object_id'],claims['object_version'],claims['request_hash'],NOW)
    reader.values[receipt.id]=receipt


def run_async(function):
    @wraps(function)
    def wrapped(*a,**kw):return asyncio.run(function(*a,**kw))
    return wrapped


def claims_for(payload):
    return {"iss":ISSUER,"aud":AUDIENCE,"caller":CALLER,"sub":payload["actor_id"],"tenant_id":payload["tenant_id"],
        "membership_id":str(uuid4()),"session_id":str(uuid4()),"access_revision":1,"action":payload["action"],"object_kind":payload["object"]["kind"],
        "object_id":payload["object"]["id"],"object_version":payload["object"]["version"],"operation_id":payload["operation_id"],
        "request_hash":request_hash(payload),"human_receipt_id":str(uuid4()),"jti":str(uuid4()),"iat":NOW,"nbf":NOW,"exp":NOW+60}


def financial_payload(action="claim.submit"):
    payload=deepcopy(FIXTURE["canonical_golden_payload"]);payload["action"]=action;payload["object"]["kind"]="draft" if action=="claim.submit" else "claim"
    r1,r2=str(uuid4()),str(uuid4())
    payload["data"]={"trip_id":str(uuid4()),"currency":"SGD","items":[{"id":str(uuid4()),"trip_item_id":str(uuid4()),"amount_minor":125,"receipt_ids":[r1,r2]},
        {"id":str(uuid4()),"trip_item_id":str(uuid4()),"amount_minor":75,"receipt_ids":[]}],"total_minor":200,
        "receipts":[{"id":r1,"version":1,"digest":"a"*64},{"id":r2,"version":2,"digest":"b"*64}],
        "policy":{"id":str(uuid4()),"version":3,"digest":"c"*64},"approval_route":[{"stage":1,"approver_id":str(uuid4()),"scope_ref":"reimbursement"}],"destination":AUDIENCE}
    if action=="claim.information":payload["data"].update(revision=2,draft={"id":str(uuid4()),"version":4})
    return payload


def test_independent_golden_canonical_hash_and_exact_reason():
    payload=FIXTURE["canonical_golden_payload"]
    assert request_hash(payload)==FIXTURE["canonical_golden_hash"]
    other=deepcopy(payload);other["data"]["reason"]=other["data"]["reason"].rstrip()
    assert request_hash(other)!=request_hash(payload)


def test_private_verification_keeps_public_shape_and_exact_token_reference(signing):
    payload=deepcopy(FIXTURE['canonical_golden_payload']);first=claims_for(payload)
    second={**first,'session_id':str(uuid4()),'jti':str(uuid4()),'human_receipt_id':str(uuid4())}
    reader=Receipts();add_receipt(reader,first);add_receipt(reader,second)
    service=DelegationService(Repository(signing.jwk),RSADelegationVerifier(),ISSUER,receipts=reader,clock=lambda:NOW)
    async def exercise():
        public=await service.verify('c'*43,signing.sign(first),payload)
        private_a=await service.verify_private('c'*43,signing.sign(first),payload)
        private_b=await service.verify_private('c'*43,signing.sign(second),payload)
        assert public==dict(private_a.actor)==dict(private_b.actor)
        assert set(public)=={'allow','scope','actor_id','tenant_id','membership_id','access_revision','action','operation_id','request_hash','verified_at','expires_at','replayed'}
        assert private_a.reference.jti==first['jti'] and private_b.reference.jti==second['jti']
        assert private_a.reference.binding_hash!=private_b.reference.binding_hash
        assert private_a.reference.credential_hash==hashlib.sha256(('eai:internal-caller:v1:'+'c'*43).encode()).hexdigest()
        assert private_a.reference.key_hash=='a'*64 and private_a.reference.key_kid=='current'
        with pytest.raises(TypeError):private_a.actor['actor_id']=str(uuid4())
        assert first['jti'] not in repr(private_a) and 'c'*43 not in repr(private_a.reference)
    asyncio.run(exercise())


def test_private_verification_rejects_catalog_and_reads_before_repository(signing):
    repo=Repository(signing.jwk);service=DelegationService(repo,RSADelegationVerifier(),ISSUER)
    for payload in [{'schema_version':2,'action':'policy.publish'},{'schema_version':1,'action':'trip.read'},{'schema_version':True,'action':'claim.submit'}]:
        with pytest.raises(DelegationFailure):asyncio.run(service.verify_private('c'*43,'untrusted',payload))
    assert repo.calls==[]


def test_items_and_receipts_reorder_stably_but_route_is_ordered():
    payload=financial_payload();changed=deepcopy(payload)
    changed["data"]["items"].reverse();changed["data"]["receipts"].reverse();changed["data"]["items"][1]["receipt_ids"].reverse()
    assert request_hash(payload)==request_hash(changed)
    assert canonical_payload(payload)==canonical_payload(changed)


@pytest.mark.parametrize("change",["amount","currency","receipt_version","receipt_digest","trip","trip_item","policy","approver","draft_version","reason","decision","stage","revision","operation"])
def test_every_material_field_changes_the_hash(change):
    payload=deepcopy(FIXTURE["canonical_golden_payload"]) if change in {"reason","decision","stage","revision"} else financial_payload()
    old=request_hash(payload)
    if change=="amount":payload["data"]["items"][0]["amount_minor"]+=1;payload["data"]["total_minor"]+=1
    if change=="currency":payload["data"]["currency"]="USD"
    if change=="receipt_version":payload["data"]["receipts"][0]["version"]+=1
    if change=="receipt_digest":payload["data"]["receipts"][0]["digest"]="d"*64
    if change=="trip":payload["data"]["trip_id"]=str(uuid4())
    if change=="trip_item":payload["data"]["items"][0]["trip_item_id"]=str(uuid4())
    if change=="policy":payload["data"]["policy"]["version"]+=1
    if change=="approver":payload["data"]["approval_route"][0]["approver_id"]=str(uuid4())
    if change=="draft_version":payload["object"]["version"]+=1
    if change=="reason":payload["data"]["reason"]+="x"
    if change=="decision":payload["data"]["decision"]="approve"
    if change=="stage":payload["data"]["stage"]+=1
    if change=="revision":payload["data"]["revision"]+=1
    if change=="operation":payload["operation_id"]=str(uuid4())
    assert request_hash(payload)!=old


@pytest.mark.parametrize("bad",["float","bool","overflow","total","extra","duplicate","receipt_missing","route","reason_blank","surrogate"])
def test_invalid_canonical_contracts_fail_closed(bad):
    payload=financial_payload()
    if bad in {"reason_blank","surrogate"}:payload=deepcopy(FIXTURE["canonical_golden_payload"]);payload["data"]["reason"]=" " if bad=="reason_blank" else "\ud800"
    if bad=="float":payload["data"]["items"][0]["amount_minor"]=1.25
    if bad=="bool":payload["object"]["version"]=True
    if bad=="overflow":payload["data"]["total_minor"]=9223372036854775808
    if bad=="total":payload["data"]["total_minor"]+=1
    if bad=="extra":payload["data"]["role"]="tenant_admin"
    if bad=="duplicate":payload["data"]["items"][1]["id"]=payload["data"]["items"][0]["id"]
    if bad=="receipt_missing":payload["data"]["receipts"].pop()
    if bad=="route":payload["data"]["approval_route"][0]["stage"]=2
    with pytest.raises((ValueError,TypeError)):request_hash(payload)


@pytest.fixture(scope="module")
def signing():return SigningFixture()


@pytest.mark.parametrize("mode",["wrong_signature","HS256","none","jku","crit","unknown_kid","duplicate_claim","duplicate_header"])
def test_signature_and_header_boundaries(signing,mode):
    claims=claims_for(FIXTURE["canonical_golden_payload"]);token=signing.sign(claims,mode)
    verifier=RSADelegationVerifier()
    if mode=="unknown_kid":assert verifier.header(token)["kid"]=="unknown"  # Registry lookup must deny it.
    else:
        with pytest.raises(Exception):verifier.verify(token,signing.jwk)


@pytest.mark.parametrize("change",[{"exp":NOW},{"iat":NOW+6,"nbf":NOW+6,"exp":NOW+60},{"exp":NOW+61},{"nbf":NOW-1},
    {"aud":[AUDIENCE]},{"access_revision":True},{"role":"approver"},{"session_id":"invalid"},{"human_receipt_id":None}])
def test_exact_claims_time_and_authority(change):
    claims=claims_for(FIXTURE["canonical_golden_payload"]);claims.update(change)
    with pytest.raises((ValueError,TypeError)):validate_claims(claims,issuer=ISSUER,audience=AUDIENCE,caller=CALLER,now=NOW)


class Repository:
    def __init__(self,jwk):self.jwk=jwk;self.calls=[]
    async def call(self,operation,payload):
        self.calls.append(operation)
        if operation=="caller":return {"status":"ok","caller_ref":"fixture","audience":AUDIENCE,"delegation_caller":CALLER}
        if operation=="key":return {"status":"ok","jwk":self.jwk,"key_hash":"a"*64}
        return {"status":"ok","access_revision":1,"verified_at":NOW,"replayed":False,"authority_expires_at":NOW+60}


@run_async
async def test_human_receipt_is_required_and_not_inferred_from_signature(signing):
    payload=deepcopy(FIXTURE["canonical_golden_payload"]);claims=claims_for(payload);repo=Repository(signing.jwk)
    token=signing.sign(claims);service=DelegationService(repo,RSADelegationVerifier(),ISSUER,clock=lambda:NOW)
    with pytest.raises(DelegationFailure) as error:await service.verify("c"*43,token,payload)
    assert error.value.status==503 and "verify" not in repo.calls
    service.receipts=Receipts()
    with pytest.raises(DelegationFailure) as error:await service.verify("c"*43,token,payload)
    assert error.value.status==403
    receipt=HumanReceipt(claims["human_receipt_id"],claims["sub"],claims["session_id"],claims["tenant_id"],claims["operation_id"],claims["action"],
                         claims["object_kind"],claims["object_id"],claims["object_version"],claims["request_hash"],NOW)
    service.receipts.values[receipt.id]=receipt
    assert (await service.verify("c"*43,token,payload))["allow"]
    with pytest.raises(ValueError):replace(receipt,object_version=True)
    service.receipts.values[receipt.id]=replace(receipt,session_id=str(uuid4()))
    with pytest.raises(DelegationFailure) as error:await service.verify("c"*43,token,payload)
    assert error.value.status==403


def test_backend_signer_and_redacted_representation(signing):
    signer=RSADelegationSigner(signing.private,"current",ISSUER,AUDIENCE,CALLER)
    claims=claims_for(FIXTURE["canonical_golden_payload"])
    assert RSADelegationVerifier().verify(signer.sign(claims,now=NOW),signing.jwk)==claims
    assert repr(signer)=="RSADelegationSigner(<redacted>)"


def test_internal_configuration_is_opt_in_and_needs_database():
    from enterprise_ai_platform.config import Settings
    assert not Settings(env="test",host="127.0.0.1",port=8000).internal_auth_enabled
    with pytest.raises(ValueError):Settings(env="test",host="127.0.0.1",port=8000,internal_auth_enabled=True)


@run_async
async def test_expiry_during_final_receipt_read_cannot_return_allow(signing):
    payload=deepcopy(FIXTURE["canonical_golden_payload"]);claims=claims_for(payload);claims["exp"]=NOW+1
    current=[NOW];repo=Repository(signing.jwk);reader=Receipts();reads=0
    receipt=HumanReceipt(claims["human_receipt_id"],claims["sub"],claims["session_id"],claims["tenant_id"],claims["operation_id"],claims["action"],
                         claims["object_kind"],claims["object_id"],claims["object_version"],claims["request_hash"],NOW)
    async def delayed(_):
        nonlocal reads
        reads+=1
        if reads==2:current[0]=NOW+2
        return receipt
    reader.get=delayed
    service=DelegationService(repo,RSADelegationVerifier(),ISSUER,receipts=reader,clock=lambda:current[0])
    with pytest.raises(DelegationFailure) as error:await service.verify("c"*43,signing.sign(claims),payload)
    assert error.value.status==401 and repo.calls[-1]=="verify"


@run_async
@pytest.mark.parametrize("flag",[False,True])
async def test_public_app_never_mounts_internal_verification_even_when_enabled(flag):
    from enterprise_ai_platform.main import create_app
    from enterprise_ai_platform.internal_api import create_internal_app
    from enterprise_ai_platform.config import Settings
    from enterprise_ai_platform.composition import ApplicationServices
    class Stub:
        async def verify(self,*args):return {"allow":True}
    settings=Settings(env="test",host="127.0.0.1",port=8000,internal_auth_enabled=flag,
                      database_enabled=flag,DATABASE_URL="postgresql://fixture:fictional@fixture.example.test/fixture?sslmode=verify-full" if flag else None)
    services=ApplicationServices(health=None,delegation=Stub())
    headers={"Authorization":"Bearer "+"c"*43,"X-EAI-Delegation":"fixture","Content-Type":"application/json"}
    for factory,status in ((create_app,404),(create_internal_app,200)):
        app=factory(settings,services=services)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://fixture.example.test") as client:
            result=await client.post("/internal/v1/actors/verify",headers=headers,json={"payload":{}})
            assert result.status_code==status


@run_async
@pytest.mark.parametrize("mode",["utf16","query","oversized"])
async def test_internal_transport_rejects_noncontract_inputs_before_service(mode):
    from enterprise_ai_platform.internal_api import create_internal_app
    from enterprise_ai_platform.config import Settings
    from enterprise_ai_platform.composition import ApplicationServices
    class Stub:
        async def verify(self,*args):pytest.fail("Invalid transport must not reach verification")
    app=create_internal_app(Settings(env="test",host="127.0.0.1",port=8000),services=ApplicationServices(health=None,delegation=Stub()))
    headers={"Authorization":"Bearer "+"c"*43,"X-EAI-Delegation":"fixture","Content-Type":"application/json"}
    body=b'{"payload":{}}'
    if mode=="utf16":body=body.decode().encode("utf-16")
    if mode=="oversized":body=b" "*131073
    url="/internal/v1/actors/verify"+("?role=approver" if mode=="query" else "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://fixture.example.test") as client:
        response=await client.post(url,headers=headers,content=body)
        assert response.status_code==400 and response.headers["cache-control"]=="no-store"


@run_async
async def test_earliest_authority_deadline_is_checked_after_db_await(signing):
    payload=deepcopy(FIXTURE["canonical_golden_payload"]);claims=claims_for(payload);repo=Repository(signing.jwk)
    receipt=HumanReceipt(claims["human_receipt_id"],claims["sub"],claims["session_id"],claims["tenant_id"],claims["operation_id"],claims["action"],
                         claims["object_kind"],claims["object_id"],claims["object_version"],claims["request_hash"],NOW)
    reader=Receipts();reader.values[receipt.id]=receipt;current=[NOW]
    original=repo.call
    async def delayed(operation,payload):
        result=await original(operation,payload)
        if operation=="verify":result["authority_expires_at"]=NOW+1;current[0]=NOW+2
        return result
    repo.call=delayed
    service=DelegationService(repo,RSADelegationVerifier(),ISSUER,receipts=reader,clock=lambda:current[0])
    with pytest.raises(DelegationFailure) as error:await service.verify("c"*43,signing.sign(claims),payload)
    assert error.value.status==401
