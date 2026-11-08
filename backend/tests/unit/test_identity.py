"""AUTH-02 HTTP guards, signed fixtures and shared-key cache expiry."""

import asyncio
import base64
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.composition import ApplicationServices
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.identity.domain import CognitoProvider, nonce_digest
from enterprise_ai_platform.modules.identity.ports import IdentityFailure
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.integrations.identity.authentication import CognitoAuthentication, FernetVerifierProtector
from enterprise_ai_platform.integrations.identity.cognito_probe import verify_id_token


ORIGIN="https://app.example.test"
ISSUER="https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test"
DOMAIN="https://fixture.auth.ap-southeast-1.amazoncognito.com"
FIXTURES=json.loads((Path(__file__).parents[1]/"contracts/identity_protocol_cases.json").read_text(encoding="utf-8"))


@pytest.fixture
def provider():
    return CognitoProvider(ISSUER,"synthetic-client","private-marker",ORIGIN+"/api/v1/auth/callback",ORIGIN,
                          DOMAIN+"/oauth2/authorize",DOMAIN+"/oauth2/token",ISSUER+"/.well-known/jwks.json")


class Repository:
    def __init__(self):self.calls=[]
    async def call(self,operation,payload):
        self.calls.append((operation,payload))
        return {"status":"ok"}


def service(provider,repo):
    return IdentityService(provider,repo,None,FernetVerifierProtector(Fernet.generate_key().decode()))


@pytest.mark.parametrize("changes",[{"auth_enabled":True},{"auth_enabled":"yes"},
    {"auth_enabled":True,"database_enabled":True,"auth_callback_logging_controlled":True}])
def test_auth_cannot_start_without_explicit_complete_safe_settings(changes):
    with pytest.raises(ValueError):Settings(env="test",host="127.0.0.1",port=8000,**changes)


@pytest.mark.parametrize("headers",[{},{"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"cross-site"},
    {"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin","Origin":"null"},
    {"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin","Origin":"https://other.test"}])
def test_csrf_bootstrap_guard_runs_before_any_database_effect(provider,headers):
    repo=Repository();identity=service(provider,repo)
    app=create_app(Settings(env="test",host="127.0.0.1",port=8000),services=ApplicationServices(health=None,identity=identity))
    with TestClient(app,base_url=ORIGIN) as client:
        response=client.get("/api/v1/me",headers={"Cookie":"__Host-eai-session="+"A"*43,**headers})
        assert response.status_code==403 and response.headers["cache-control"]=="no-store"
    assert repo.calls==[]


def test_duplicate_cookies_never_select_an_arbitrary_session(provider):
    repo=Repository();identity=service(provider,repo)
    app=create_app(Settings(env="test",host="127.0.0.1",port=8000),services=ApplicationServices(health=None,identity=identity))
    with TestClient(app,base_url=ORIGIN) as client:
        result=client.get("/api/v1/me",headers={"Cookie":"__Host-eai-session="+"A"*43+"; __Host-eai-session="+"B"*43,
            "X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})
        assert result.status_code==401 and "Max-Age=0" in result.headers["set-cookie"]
    assert repo.calls==[]


def test_login_redirect_pkce_binding_and_independent_limits(provider):
    repo=Repository();identity=service(provider,repo)
    result=asyncio.run(identity.login("home","A"*43,"198.51.100.1"))
    query=parse_qs(result["location"].split("?",1)[1])
    assert query["code_challenge_method"]==["S256"] and "code_verifier" not in query
    assert len(query["state"][0])==len(query["nonce"][0])==len(result["binding"])==43
    assert [op for op,_ in repo.calls]==["limit","login_start"]
    assert len(repo.calls[0][1]["keys"])==2
    persisted=repo.calls[1][1]
    assert persisted["verifier"]!=query["code_challenge"][0]
    assert query["state"][0] not in str(persisted) and query["nonce"][0] not in str(persisted)


def test_bad_callback_counts_request_without_outbound_exchange(provider):
    class Denied(Repository):
        async def call(self,operation,payload):
            self.calls.append((operation,payload))
            return {"status":"ok"} if operation=="limit" else {"status":"unauthenticated"}
    repo=Denied();identity=service(provider,repo)
    with pytest.raises(IdentityFailure) as error:
        asyncio.run(identity.callback(b"code=private-marker&state="+b"B"*43,"A"*43,None,"198.51.100.1"))
    assert error.value.status==401 and [x[0] for x in repo.calls]==["limit","login_claim"]


def test_cache_fallback_rechecks_age_after_failed_refresh(provider):
    clock=[3599.0]
    class Cache:
        def __init__(self):self.calls=[]
        async def call(self,operation,payload):
            self.calls.append(operation)
            if operation=="jwks_failure":return {"status":"ok"}
            return {"status":"ok","refresh":len(self.calls)==1,"inflight":False,
                    "valid_until":3600,"keys":[{"kid":"known"}] if clock[0]<3600 else []}
    async def unavailable(request):
        clock[0]+=5
        raise httpx.ReadTimeout("private-marker")
    repo=Cache();auth=CognitoAuthentication(provider,repo,transport=httpx.MockTransport(unavailable),cache_clock=lambda:clock[0])
    with pytest.raises(IdentityFailure):asyncio.run(auth.keys("known"))
    assert repo.calls==["jwks_reserve","jwks_failure","jwks_reserve"]


def test_even_cached_snapshot_is_rejected_at_exact_hour_boundary(provider):
    class Cache:
        async def call(self,*args):return {"status":"ok","refresh":False,"inflight":False,"keys":[{"kid":"known"}],"valid_until":3600}
    auth=CognitoAuthentication(provider,Cache(),cache_clock=lambda:3600)
    assert asyncio.run(auth.keys("known"))==[]


def test_transport_loggers_created_after_security_setup_are_redacted(caplog):
    from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
    safe_transport_logs()
    caplog.set_level(logging.DEBUG)
    logger=logging.getLogger("botocore.new_dynamic_service.fixture")
    logger.debug("private-marker %s", "private-marker")
    assert "private-marker" not in caplog.text


@pytest.fixture(scope="module")
def signing():
    private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    key={**jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(),as_dict=True),"kid":"known","use":"sig","alg":"RS256"}
    return private,key


OIDC_CASES=[c for c in FIXTURES["cases"] if c["layer"]=="oidc" and not any(k in c["changes"] for k in ("signature","alg","duplicate_json_claim"))]


@pytest.mark.parametrize("case",OIDC_CASES,ids=[c["id"] for c in OIDC_CASES])
def test_sec06_claim_cases_use_real_signed_tokens(provider,signing,case):
    claims={**FIXTURES["baselines"]["id_claims"],"iss":ISSUER}
    for k,v in case["changes"].items():
        if v is None:claims.pop(k,None)
        else:claims[k]=v
    token=jwt.encode(claims,signing[0],algorithm="RS256",headers={"kid":"known"})
    def run():verify_id_token(token,[signing[1]],provider,nonce_digest("A"*43),FIXTURES["clock"])
    if case["expected"]=="accept_valid_signed_token":run()
    else:
        with pytest.raises(ValueError):run()
