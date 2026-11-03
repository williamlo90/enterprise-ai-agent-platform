"""Controlled provider/store fixtures; these tests do not claim live Cognito login."""

import asyncio
from dataclasses import replace
import json
import logging
import secrets
import time
from urllib.parse import parse_qs, urlencode

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from enterprise_ai_platform.config import ConfigurationError, load_oidc_probe_settings
from enterprise_ai_platform.infrastructure.oidc_spike_store import StoreRejected
from enterprise_ai_platform.integrations.identity.cognito_probe import (
    CognitoProbe, ProbeUnavailable, strict_json, validate_keys, verify_id_token,
)
from enterprise_ai_platform.modules.identity.domain import CognitoProvider, OidcRejected, nonce_digest
from enterprise_ai_platform.oidc_spike_lambda import COOKIE, SpikeApplication, handler


ORIGIN = "https://abc123.lambda-url.ap-southeast-1.on.aws"
ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test"
DOMAIN = "https://example.auth.ap-southeast-1.amazoncognito.com"
NOW = 1800000000
NONCE = "A" * 43


@pytest.fixture
def provider():
    return CognitoProvider(ISSUER, "synthetic-client", "synthetic-private-secret", ORIGIN + "/api/v1/auth/callback",
                           ORIGIN, DOMAIN + "/oauth2/authorize", DOMAIN + "/oauth2/token",
                           ISSUER + "/.well-known/jwks.json")


@pytest.fixture(scope="module")
def signing():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return private, {**key, "kid": "test-key", "alg": "RS256", "use": "sig"}


def claims():
    return {"iss": ISSUER, "sub": "fictional-user", "aud": "synthetic-client", "token_use": "id",
            "iat": NOW - 10, "exp": NOW + 300, "nonce": NONCE}


def encoded(claims_value, signing):
    return jwt.encode(claims_value, signing[0], algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.parametrize("delta", [
    {"iss": ISSUER + "/"}, {"aud": "other"}, {"azp": "other"}, {"aud": ["synthetic-client", "other"]},
    {"aud": ["synthetic-client", "synthetic-client"]}, {"token_use": "access"},
    {"nonce": "B" * 43}, {"nonce": None}, {"sub": ""}, {"sub": True},
    {"exp": NOW - 60, "iat": NOW - 70}, {"iat": NOW - 661}, {"iat": NOW + 61},
    {"nbf": NOW + 61}, {"exp": True}, {"iat": "1800000000"}, {"exp": NOW - 10},
])
def test_real_signatures_do_not_bypass_negative_claims(provider, signing, delta):
    with pytest.raises(OidcRejected):
        verify_id_token(encoded({**claims(), **delta}, signing), [signing[1]], provider, nonce_digest(NONCE), NOW)


@pytest.mark.parametrize("delta", [
    {}, {"exp": NOW - 59, "iat": NOW - 70}, {"iat": NOW - 660}, {"iat": NOW + 60},
    {"nbf": NOW + 60}, {"aud": ["synthetic-client", "other"], "azp": "synthetic-client"},
])
def test_real_signed_positive_and_exact_boundaries(provider, signing, delta):
    assert verify_id_token(encoded({**claims(), **delta}, signing), [signing[1]], provider, nonce_digest(NONCE), NOW) is None


def test_wrong_signature_algorithm_and_duplicate_keys_rejected(provider, signing):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong = jwt.encode(claims(), other, algorithm="RS256", headers={"kid": "test-key"})
    unsigned = jwt.encode(claims(), key="", algorithm="none", headers={"kid": "test-key"})
    for token in (wrong, unsigned, "invalid", "x" * 16385):
        with pytest.raises(OidcRejected):
            verify_id_token(token, [signing[1]], provider, nonce_digest(NONCE), NOW)
    with pytest.raises(OidcRejected):
        strict_json(b'{"aud":"a","aud":"b"}')
    with pytest.raises(OidcRejected):
        validate_keys({"keys": [signing[1], signing[1]]})


@pytest.mark.parametrize("field,value", [
    ("token_endpoint", "https://attacker.invalid/token"), ("jwks_uri", "https://attacker.invalid/jwks"),
    ("redirect_uri", ORIGIN + "/wrong"), ("public_origin", "http://localhost"), ("client_secret", ""),
])
def test_unapproved_endpoints_and_unsafe_config_rejected(provider, field, value):
    with pytest.raises(OidcRejected):
        replace(provider, **{field: value})
    assert "synthetic-private-secret" not in repr(provider)


def test_missing_probe_settings_are_sanitized(tmp_path):
    p = tmp_path / "config.env"
    p.write_text("OIDC_CLIENT_SECRET=synthetic-private-marker\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        load_oidc_probe_settings(p)
    assert "synthetic-private-marker" not in str(error.value)


def test_provider_preflight_and_failure_redaction(provider, signing, caplog):
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "authorization_endpoint": provider.authorization_endpoint,
                "token_endpoint": provider.token_endpoint, "jwks_uri": provider.jwks_uri,
                "response_types_supported": ["code"], "id_token_signing_alg_values_supported": ["RS256"]})
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(200, json={"keys": [signing[1]]})
        assert request.headers["Authorization"].startswith("Basic ")
        assert "synthetic-invalid-" in request.content.decode()
        return httpx.Response(400, json={"error": "invalid_grant"})
    caplog.set_level(logging.DEBUG)
    result = asyncio.run(CognitoProbe(provider, transport=httpx.MockTransport(respond)).preflight(check_client=True))
    assert all(result.values()) and len(requests) == 3
    assert "synthetic-private-secret" not in caplog.text
    probe = CognitoProbe(provider, transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "https://attacker.invalid"})))
    with pytest.raises(ProbeUnavailable):
        asyncio.run(probe.preflight())


class MemoryStore:
    """Fault-controlled stand-in; DynamoDB atomicity requires separate live evidence."""
    def __init__(self):
        self.rows, self.claims = {}, 0
    def limit(self, now):
        pass
    def start(self, row, old):
        self.rows.pop(old, None)
        self.rows[row["pk"]] = dict(row)
    def claim(self, key, state, now):
        row = self.rows.get(key)
        if not row or row["status"] != "pending" or row["state_hash"] != state or row["expires_at"] <= now:
            raise StoreRejected()
        before = dict(row)
        row["status"] = "consumed"
        row.pop("verifier")
        self.claims += 1
        return before
    def finish(self, key, success):
        self.rows[key]["status"] = "verified" if success else "failed"
    def result(self, key, now):
        row = self.rows.get(key)
        if not row or row["expires_at"] <= now:
            raise StoreRejected()
        return row["status"] == "verified"


def event(path, *, method="GET", cookies=None, query="", headers=None, body=None):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "rawQueryString": query,
            "headers": headers or {}, "cookies": cookies or [], "body": body}


def start(application, operator):
    return asyncio.run(application.handle(event("/spike/start", method="POST", body="{}", headers={
        "origin": ORIGIN, "sec-fetch-site": "same-origin", "content-type": "application/json",
        "x-eai-spike-operator": operator})))


def test_two_app_instances_share_once_only_claim_and_browser_result(provider, signing):
    store, operator, encryption_key = MemoryStore(), secrets.token_urlsafe(32), Fernet.generate_key()
    calls = []
    nonce = None
    class Provider:
        async def request(self, method, url, **kwargs):
            calls.append(url)
            await asyncio.sleep(0)
            assert all("verifier" not in row for row in store.rows.values())
            if method == "POST":
                return 200, {"id_token": encoded({**claims(), "nonce": nonce}, signing)}
            return 200, {"keys": [signing[1]]}
    first = SpikeApplication(provider, operator, encryption_key, store, probe=Provider(), clock=lambda: NOW)
    second = SpikeApplication(provider, operator, encryption_key, store, probe=Provider(), clock=lambda: NOW)
    launched = start(first, operator)
    params = parse_qs(json.loads(launched["body"])["authorization_url"].split("?", 1)[1])
    nonce = params["nonce"][0]
    assert params["code_challenge_method"] == ["S256"] and "code_verifier" not in params
    cookie = launched["cookies"][0].split(";", 1)[0]
    callback = event("/api/v1/auth/callback", cookies=[cookie], query=urlencode({"state": params["state"][0], "code": "synthetic-code"}))
    async def simultaneous():
        return await asyncio.gather(first.handle(callback), second.handle(callback), return_exceptions=True)
    results = asyncio.run(simultaneous())
    assert sum(isinstance(item, StoreRejected) for item in results) == 1
    assert store.claims == 1 and len(calls) == 2
    result = asyncio.run(second.handle(event("/spike/result", cookies=[cookie])))
    assert json.loads(result["body"]) == {"provider_verified": True, "application_session_created": False}
    assert asyncio.run(first.handle(event("/spike/result")))["statusCode"] == 401


def test_wrong_state_binding_duplicates_and_expiry_never_fetch_provider(provider):
    store, operator = MemoryStore(), secrets.token_urlsafe(32)
    application = SpikeApplication(provider, operator, Fernet.generate_key(), store, clock=lambda: NOW)
    launched = start(application, operator)
    params = parse_qs(json.loads(launched["body"])["authorization_url"].split("?", 1)[1])
    cookie = launched["cookies"][0].split(";", 1)[0]
    assert asyncio.run(application.handle(event("/api/v1/auth/callback", query="code=x&state=" + "B" * 43)))["statusCode"] == 400
    assert asyncio.run(application.handle(event("/api/v1/auth/callback", cookies=[cookie], query="code=x&code=y&state=" + "B" * 43)))["statusCode"] == 400
    with pytest.raises(StoreRejected):
        asyncio.run(application.handle(event("/api/v1/auth/callback", cookies=[cookie], query="code=x&state=" + "B" * 43)))
    assert store.claims == 0
    application.clock = lambda: NOW + 300
    with pytest.raises(StoreRejected):
        asyncio.run(application.handle(event("/api/v1/auth/callback", cookies=[cookie], query="code=x&state=" + params["state"][0])))
    assert store.claims == 0


def test_disabled_lambda_never_reads_credentials_or_logs_input(monkeypatch, caplog):
    monkeypatch.delenv("SPIKE_ENABLED", raising=False)
    result = handler({"rawQueryString": "code=synthetic-private-marker"}, None)
    assert result["statusCode"] == 404 and "synthetic-private-marker" not in caplog.text


@pytest.mark.parametrize("name", ["iss", "sub", "aud", "exp", "iat", "nonce", "token_use"])
def test_missing_required_claims_rejected(provider, signing, name):
    value = claims(); value.pop(name)
    with pytest.raises(OidcRejected):
        verify_id_token(encoded(value, signing), [signing[1]], provider, nonce_digest(NONCE), NOW)


def test_provider_outage_after_claim_is_terminal_without_verifier_reuse(provider):
    class FailingProvider:
        calls = 0
        async def request(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("synthetic-private-provider-error")
    store, operator, probe = MemoryStore(), secrets.token_urlsafe(32), FailingProvider()
    application = SpikeApplication(provider, operator, Fernet.generate_key(), store, probe=probe, clock=lambda: NOW)
    launched = start(application, operator)
    params = parse_qs(json.loads(launched["body"])["authorization_url"].split("?", 1)[1])
    cookie = launched["cookies"][0].split(";", 1)[0]
    callback = event("/api/v1/auth/callback", cookies=[cookie], query="code=synthetic&state=" + params["state"][0])
    result = asyncio.run(application.handle(callback))
    assert result["statusCode"] == 303 and result["headers"]["location"] == "/spike/result"
    assert "synthetic-private" not in json.dumps(result)
    assert list(store.rows.values())[0]["status"] == "failed"
    assert "verifier" not in list(store.rows.values())[0]
    with pytest.raises(StoreRejected):
        asyncio.run(application.handle(callback))
    assert probe.calls == 1


@pytest.mark.parametrize("header,value", [("origin", "https://attacker.invalid"),
    ("sec-fetch-site", "same-site"), ("x-eai-spike-operator", "wrong"), ("content-type", "text/plain")])
def test_launcher_rejects_without_changing_store(provider, header, value):
    store, operator = MemoryStore(), secrets.token_urlsafe(32)
    app = SpikeApplication(provider, operator, Fernet.generate_key(), store, clock=lambda: NOW)
    headers = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "content-type": "application/json",
               "x-eai-spike-operator": operator, header: value}
    result = asyncio.run(app.handle(event("/spike/start", method="POST", body="{}", headers=headers)))
    assert result["statusCode"] == 403 and store.rows == {}


def test_http_redirect_oversize_and_bad_json_fail_safely(provider):
    for response in [httpx.Response(200, content=b"x" * 262145),
                     httpx.Response(200, content=b'{"a":1,"a":2}'),
                     httpx.Response(500, content=b"synthetic-private-error")]:
        probe = CognitoProbe(provider, transport=httpx.MockTransport(lambda _, r=response: r))
        with pytest.raises(ProbeUnavailable) as error:
            asyncio.run(probe.preflight())
        assert "synthetic-private-error" not in str(error.value)


def test_dynamodb_claim_commits_removal_and_fails_closed_on_ambiguous_error():
    import boto3
    from botocore.stub import Stubber
    from enterprise_ai_platform.infrastructure.oidc_spike_store import DynamoSpikeStore
    client = boto3.client("dynamodb", region_name="ap-southeast-1",
                          aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
    expected = {
        "TableName": "eai-oidc-spike-test", "Key": {"pk": {"S": "tx#test"}},
        "UpdateExpression": "SET #s=:used REMOVE verifier",
        "ConditionExpression": "#s=:pending AND state_hash=:state AND expires_at > :now AND attribute_exists(verifier)",
        "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": {
            ":used": {"S": "consumed"}, ":pending": {"S": "pending"}, ":state": {"S": "hash"}, ":now": {"N": "1800000000"}},
        "ReturnValues": "ALL_OLD",
    }
    with Stubber(client) as stub:
        stub.add_response("update_item", {"Attributes": {"verifier": {"S": "encrypted"}}}, expected)
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException", expected_params=expected)
        store = DynamoSpikeStore("eai-oidc-spike-test", client=client)
        assert store.claim("tx#test", "hash", NOW)["verifier"] == "encrypted"
        with pytest.raises(StoreRejected):
            store.claim("tx#test", "hash", NOW)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("query", ["&".join(f"x{i}=v" for i in range(11)), "state=%FF", "state=%QZ"])
def test_malformed_callback_is_400_before_store(provider, query):
    app = SpikeApplication(provider, secrets.token_urlsafe(32), Fernet.generate_key(), MemoryStore())
    assert asyncio.run(app.handle(event("/api/v1/auth/callback", query=query)))["statusCode"] == 400


@pytest.mark.parametrize("body", ["***not-base64***", "/w=="])
def test_invalid_base64_and_utf8_launcher_body_is_400(provider, body):
    operator, store = secrets.token_urlsafe(32), MemoryStore()
    app = SpikeApplication(provider, operator, Fernet.generate_key(), store)
    request = event("/spike/start", method="POST", body=body, headers={"origin": ORIGIN,
        "sec-fetch-site": "same-origin", "content-type": "application/json", "x-eai-spike-operator": operator})
    request["isBase64Encoded"] = True
    assert asyncio.run(app.handle(request))["statusCode"] == 400 and not store.rows


def test_joint_deadline_prevents_second_provider_call_and_reserves_finalization(provider):
    from enterprise_ai_platform.modules.identity.domain import ProbeDeadline
    clock, calls = [0.0], []
    class TimedStore(MemoryStore):
        def limit(self, now):
            clock[0] += 4
        def claim(self, *args):
            clock[0] += 4
            return super().claim(*args)
        def finish(self, *args):
            clock[0] += 1
            super().finish(*args)
    class TimedProvider:
        async def request(self, method, *args, **kwargs):
            calls.append(method)
            clock[0] += 14
            return 200, {"id_token": "must-not-reach-jwks"}
    store, operator = TimedStore(), secrets.token_urlsafe(32)
    app = SpikeApplication(provider, operator, Fernet.generate_key(), store,
        probe=TimedProvider(), deadline=ProbeDeadline(24, lambda: clock[0]), clock=lambda: NOW)
    launched = start(app, operator)
    clock[0] = 0
    params = parse_qs(json.loads(launched["body"])["authorization_url"].split("?", 1)[1])
    cookie = launched["cookies"][0].split(";", 1)[0]
    result = asyncio.run(app.handle(event("/api/v1/auth/callback", cookies=[cookie],
        query="code=synthetic&state=" + params["state"][0])))
    assert result["statusCode"] == 303 and calls == ["POST"] and clock[0] == 23
    assert list(store.rows.values())[0]["status"] == "failed"


def test_dynamodb_expired_shared_deadline_never_calls_sdk():
    from enterprise_ai_platform.modules.identity.domain import ProbeDeadline
    from enterprise_ai_platform.infrastructure.oidc_spike_store import DynamoSpikeStore
    class UnexpectedClient:
        def update_item(self, **kwargs):
            raise AssertionError("SDK must not be called after deadline")
    store = DynamoSpikeStore("eai-oidc-spike-test", client=UnexpectedClient(), deadline=ProbeDeadline(0, lambda: 1))
    with pytest.raises(StoreRejected):
        store.claim("key", "hash", NOW)


def test_store_rate_rollover_budget_atomicity_and_finish_failure():
    import boto3
    from botocore.stub import ANY, Stubber
    from enterprise_ai_platform.infrastructure.oidc_spike_store import DynamoSpikeStore
    client = boto3.client("dynamodb", region_name="ap-southeast-1",
                          aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
    store = DynamoSpikeStore("eai-oidc-spike-test", client=client)
    with Stubber(client) as stub:
        stub.add_response("update_item", {}, {"TableName": store.table, "Key": {"pk": {"S": "rate"}},
            "UpdateExpression": "SET #w=:window, #c=:one", "ConditionExpression": "attribute_not_exists(pk) OR #w < :window",
            "ExpressionAttributeNames": {"#w": "window", "#c": "requests"},
            "ExpressionAttributeValues": {":window": {"N": str(NOW // 60)}, ":one": {"N": "1"}}})
        store.limit(NOW)
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        with pytest.raises(StoreRejected):
            store.limit(NOW)
        stub.add_client_error("transact_write_items", service_error_code="TransactionCanceledException",
                              expected_params={"TransactItems": [ANY, ANY, ANY]})
        with pytest.raises(StoreRejected):
            store.start({"pk": "tx#new", "expires_at": NOW + 300}, "tx#old")
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        with pytest.raises(StoreRejected):
            store.finish("tx#new", True)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("phase", ["limiter", "claim"])
def test_expiry_crossed_during_store_work_prevents_all_provider_calls(provider, phase):
    clock, slow = [NOW], [False]
    class SlowStore(MemoryStore):
        def limit(self, now):
            if slow[0] and phase == "limiter":
                clock[0] += 2
        def claim(self, *args):
            row = super().claim(*args)
            if slow[0] and phase == "claim":
                clock[0] += 2
            return row
    class UnexpectedProvider:
        async def request(self, *args, **kwargs):
            pytest.fail("Expired transaction must cause zero provider requests")
    store, operator = SlowStore(), secrets.token_urlsafe(32)
    app = SpikeApplication(provider, operator, Fernet.generate_key(), store,
                            probe=UnexpectedProvider(), clock=lambda: clock[0])
    launched = start(app, operator)
    params = parse_qs(json.loads(launched["body"])["authorization_url"].split("?", 1)[1])
    cookie = launched["cookies"][0].split(";", 1)[0]
    clock[0], slow[0] = NOW + 299, True
    callback = event("/api/v1/auth/callback", cookies=[cookie],
                      query="code=synthetic&state=" + params["state"][0])
    if phase == "limiter":
        with pytest.raises(StoreRejected):
            asyncio.run(app.handle(callback))
        assert store.claims == 0
    else:
        assert asyncio.run(app.handle(callback))["statusCode"] == 303
        assert store.claims == 1 and list(store.rows.values())[0]["status"] == "failed"
