"""Offline tests of the user-run checkers; never use AWS credentials or endpoints."""

import json
from pathlib import Path
import runpy

import httpx
import pytest


SCRIPTS = Path(__file__).resolve().parents[3] / "scripts/spikes"


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return runpy.run_path(str(SCRIPTS / "check_oidc_runtime.py"))


def test_pre_enable_proof_is_time_and_configuration_bound(checker):
    proof = {"phase": "pre-enable", "passed": True, "scoped_iam_simulation": True,
             "configuration_fingerprint": "hash", "checked_at": 100}
    checker["check_prerequisite"](proof, "hash", 3700)
    for fingerprint, now in [("other", 101), ("hash", 3701), ("hash", 99)]:
        with pytest.raises(ValueError):
            checker["check_prerequisite"](proof, fingerprint, now)
    with pytest.raises(ValueError):
        checker["check_prerequisite"]({**proof, "passed": False}, "hash", 101)


def test_simulation_reads_all_pages_and_rejects_missing_context(checker):
    class IAM:
        def __init__(self):
            self.calls = []
        def simulate_principal_policy(self, **values):
            self.calls.append(values)
            action = "dynamodb:GetItem" if not values.get("Marker") else "dynamodb:PutItem"
            return {"EvaluationResults": [{"EvalActionName": action, "EvalResourceName": "table",
                                            "EvalDecision": "allowed"}],
                    "IsTruncated": not bool(values.get("Marker")), "Marker": "next"}
    iam = IAM()
    checker["simulate"](iam, "role", ["dynamodb:GetItem", "dynamodb:PutItem"], "table", True)
    assert len(iam.calls) == 2
    class Incomplete:
        def simulate_principal_policy(self, **values):
            return {"EvaluationResults": [{"EvalActionName": "x", "EvalResourceName": "resource",
                "EvalDecision": "allowed", "MissingContextValues": ["unknown"]}]}
    with pytest.raises(ValueError):
        checker["simulate"](Incomplete(), "role", ["x"], "resource", True)


@pytest.mark.parametrize("top_resource", ["*", "arn:${Partition}:logs:${Region}:${Account}:log-group:${Group}:log-stream:${Stream}"])
def test_simulation_uses_exact_resource_specific_result(checker, top_resource):
    resource = "arn:aws:logs:ap-southeast-1:111122223333:log-group:/aws/lambda/probe:log-stream:probe-check"
    class IAM:
        def simulate_principal_policy(self, **values):
            assert values["ResourceArns"] == [resource]
            return {"EvaluationResults": [{"EvalActionName": "logs:PutLogEvents",
                "EvalResourceName": top_resource, "EvalDecision": "allowed",
                "ResourceSpecificResults": [{"EvalResourceName": resource, "EvalResourceDecision": "allowed"}]}]}
    checker["simulate"](IAM(), "role", ["logs:PutLogEvents"], resource, True)


@pytest.mark.parametrize("specific", [[],
    [{"EvalResourceName": "foreign", "EvalResourceDecision": "allowed"}],
    [{"EvalResourceName": "own", "EvalResourceDecision": "implicitDeny"}],
    [{"EvalResourceName": "own", "EvalResourceDecision": "allowed", "MissingContextValues": ["required"]}],
    [{"EvalResourceName": "own", "EvalResourceDecision": "allowed"}] * 2,
])
def test_simulation_rejects_invalid_specific_results(checker, specific):
    class IAM:
        def simulate_principal_policy(self, **values):
            return {"EvaluationResults": [{"EvalActionName": "logs:PutLogEvents", "EvalResourceName": "own",
                "EvalDecision": "allowed", "ResourceSpecificResults": specific}]}
    with pytest.raises(ValueError):
        checker["simulate"](IAM(), "role", ["logs:PutLogEvents"], "own", True)


def test_simulation_never_accepts_allowed_foreign_resource(checker):
    class Broad:
        def simulate_principal_policy(self, **values):
            return {"EvaluationResults": [{"EvalActionName": "x", "EvalResourceName": "foreign", "EvalDecision": "allowed"}]}
    with pytest.raises(ValueError):
        checker["simulate"](Broad(), "role", ["x"], "foreign", False)


@pytest.mark.parametrize("broad_logs", [False, True])
def test_scope_checks_require_own_logs_and_deny_foreign_group(checker, broad_logs):
    own = "arn:aws:logs:ap-southeast-1:111122223333:log-group:/aws/lambda/probe:*"
    seen = []
    class IAM:
        def simulate_principal_policy(self, **values):
            resource = values["ResourceArns"][0]
            seen.append(resource)
            allowed = resource != "*" and "forbidden" not in resource
            if broad_logs and resource.startswith("arn:aws:logs:"):
                allowed = True
            return {"EvaluationResults": [{"EvalActionName": action, "EvalResourceName": resource,
                "EvalDecision": "allowed" if allowed else "implicitDeny"} for action in values["ActionNames"]]}
    class Session:
        def client(self, service):
            assert service == "iam"
            return IAM()
    args = (Session(), {"account": "111122223333", "outputs": {"LogGroup": "/aws/lambda/probe"}},
            {"Role": "role"}, {"SPIKE_TABLE_NAME": "table", "SPIKE_SECRET_ARN": "secret"})
    if broad_logs:
        with pytest.raises(ValueError):
            checker["check_iam_scope"](*args)
    else:
        checker["check_iam_scope"](*args)
    assert own in seen and own.replace("probe:*", "probe-forbidden:*") in seen


def test_log_scan_checks_all_markers_without_echoing_them(checker):
    checker["require_clean_logs"](["START RequestId: id", "REPORT RequestId: id"], ["private-marker"])
    with pytest.raises(ValueError) as error:
        checker["require_clean_logs"](["some private-marker"], ["private-marker"])
    assert "private-marker" not in str(error.value)


def test_correlated_logs_require_all_three_events_and_all_pages(checker):
    class Paginator:
        def paginate(self, **kwargs):
            return [{"events": [{"message": "START RequestId: ours"}]},
                    {"events": [{"message": "END RequestId: ours"}, {"message": "REPORT RequestId: ours"}]}]
    class Logs:
        def get_paginator(self, name):
            assert name == "filter_log_events"
            return Paginator()
    messages = checker["collect_complete_logs"](Logs(), "group", 1, "ours", pause=lambda _: None)
    assert len(messages) == 3
    class Partial(Paginator):
        def paginate(self, **kwargs):
            return [{"events": [{"message": "START RequestId: ours"}, {"message": "REPORT RequestId: other"}]}]
    class PartialLogs(Logs):
        def get_paginator(self, name):
            return Partial()
    with pytest.raises(ValueError):
        checker["collect_complete_logs"](PartialLogs(), "group", 1, "ours", pause=lambda _: None)


def test_authorization_validation_does_not_visit_provider(checker):
    from urllib.parse import urlencode
    values = {"OIDC_AUTHORIZATION_ENDPOINT": "https://login.example.test/oauth2/authorize",
              "OIDC_REDIRECT_URI": "https://probe.example.test/api/v1/auth/callback", "OIDC_CLIENT_ID": "client"}
    params = {"response_type": "code", "client_id": "client", "redirect_uri": values["OIDC_REDIRECT_URI"],
              "scope": "openid profile", "state": "A" * 43, "nonce": "B" * 43,
              "code_challenge": "C" * 43, "code_challenge_method": "S256"}
    url = values["OIDC_AUTHORIZATION_ENDPOINT"] + "?" + urlencode(params)
    assert checker["authorization"](httpx.Response(200, json={"authorization_url": url}), values)["state"] == ["A" * 43]
    with pytest.raises(ValueError):
        checker["authorization"](httpx.Response(200, json={"authorization_url": url.replace("login.example.test", "other.example.test")}), values)


def test_enabled_checker_drives_runtime_with_dummy_inputs_and_cleans_only_owned_rows(checker, monkeypatch, tmp_path):
    import asyncio
    import time
    from uuid import uuid4
    from boto3.dynamodb.types import TypeSerializer
    from cryptography.fernet import Fernet
    from enterprise_ai_platform.infrastructure.oidc_spike_store import StoreRejected
    from enterprise_ai_platform.modules.identity.domain import CognitoProvider
    from enterprise_ai_platform.oidc_spike_lambda import SpikeApplication, response
    origin = "https://abc123.lambda-url.ap-southeast-1.on.aws"
    issuer = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test"
    domain = "https://example.auth.ap-southeast-1.amazoncognito.com"
    provider = CognitoProvider(issuer, "client", "synthetic-secret", origin + "/api/v1/auth/callback",
        origin, domain + "/oauth2/authorize", domain + "/oauth2/token", issuer + "/.well-known/jwks.json")
    operator = "X" * 43
    class Store:
        rows = {}
        def limit(self, now): pass
        def start(self, row, old):
            self.rows.pop(old, None); self.rows[row["pk"]] = dict(row)
        def claim(self, key, state, now):
            row = self.rows.get(key)
            if not row or row["status"] != "pending" or row["state_hash"] != state:
                raise StoreRejected()
            before = dict(row); row["status"] = "consumed"; row.pop("verifier")
            return before
        def finish(self, key, success): self.rows[key]["status"] = "verified" if success else "failed"
        def result(self, key, now): return self.rows[key]["status"] == "verified"
    store = Store()
    class NoProvider:
        async def request(self, *args, **kwargs): pytest.fail("Canary must not contact Cognito")
    app = SpikeApplication(provider, operator, Fernet.generate_key(), store, probe=NoProvider())
    def serve(request):
        assert str(request.url).startswith(origin + "/")
        event = {"rawPath": request.url.path, "rawQueryString": request.url.query.decode(),
            "headers": dict(request.headers), "body": request.content.decode(),
            "cookies": [request.headers.get("cookie", "")], "requestContext": {"http": {"method": request.method}}}
        try: value = asyncio.run(app.handle(event))
        except StoreRejected: value = response(503, {"status": "unavailable"})
        headers = {**value["headers"], "x-amzn-requestid": str(uuid4())}
        if value.get("cookies"): headers["set-cookie"] = value["cookies"][0]
        return httpx.Response(value["statusCode"], headers=headers, content=value["body"])
    class DDB:
        cleaned = []
        def get_item(self, **values):
            row = store.rows.get(values["Key"]["pk"]["S"], {})
            return {"Item": {k: TypeSerializer().serialize(v) for k, v in row.items()}}
        def delete_item(self, **values):
            key = values["Key"]["pk"]["S"]
            assert store.rows[key]["state_hash"] == values["ExpressionAttributeValues"][":state"]["S"]
            self.cleaned.append(key); del store.rows[key]
    ddb = DDB()
    class Session:
        def client(self, name):
            assert name in {"dynamodb", "logs"}
            return ddb if name == "dynamodb" else object()
    folder = tmp_path / "local/oidc"; folder.mkdir(parents=True)
    (folder / "aws-spike-operator.key").write_text(operator)
    proof = folder / "pre.json"
    proof.write_text(json.dumps({"phase": "pre-enable", "passed": True, "scoped_iam_simulation": True,
        "configuration_fingerprint": "fingerprint", "checked_at": int(time.time())}))
    namespace = checker["enabled_canary"].__globals__
    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    monkeypatch.setitem(namespace, "PRE_FILE", proof)
    monkeypatch.setitem(namespace, "collect_complete_logs", lambda *args: ["metadata only"])
    iam_checks = []
    monkeypatch.setitem(namespace, "check_iam_scope", lambda *args: iam_checks.append(True))
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(serve), **kwargs))
    result = checker["enabled_canary"](Session(), {"outputs": {"LogGroup": "group"}}, {"Role": "role"}, {
        "SPIKE_TABLE_NAME": "table", "OIDC_CLIENT_ID": "client", "OIDC_REDIRECT_URI": provider.redirect_uri,
        "OIDC_AUTHORIZATION_ENDPOINT": provider.authorization_endpoint}, origin, "fingerprint")
    assert result["passed"] is True and result["invocations_checked"] == 8
    assert result["actual_user_login"] == "not_tested" and len(ddb.cleaned) == 1 and not store.rows
    assert iam_checks == [True]


@pytest.mark.parametrize("suffix", [
    "; HttpOnly; SameSite=Lax; Path=/; Max-Age=300",
    "; Secure; SameSite=Lax; Path=/; Max-Age=300",
    "; Secure; HttpOnly; Path=/; Max-Age=300",
    "; Secure; HttpOnly; SameSite=None; Path=/; Max-Age=300",
    "; Secure; HttpOnly; SameSite=Lax; Path=/other; Max-Age=300",
    "; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=301",
    "; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=300; Domain=example.test",
    "; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=300; Domain=",
])
def test_missing_or_wrong_cookie_attributes_are_rejected(checker, suffix):
    with pytest.raises(ValueError):
        checker["secure_cookie"](httpx.Response(200, headers={"set-cookie": "__Host-eai-spike=" + "A" * 43 + suffix}))


def test_extra_session_cookie_is_rejected(checker):
    value = "__Host-eai-spike=" + "A" * 43 + "; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=300"
    with pytest.raises(ValueError):
        checker["secure_cookie"](httpx.Response(200, headers=[("set-cookie", value), ("set-cookie", "session=unexpected")]))


def test_changed_policy_on_same_role_stops_enabled_before_operator_or_http(checker, monkeypatch, tmp_path):
    import time
    proof = tmp_path / "pre.json"
    proof.write_text(json.dumps({"phase": "pre-enable", "passed": True, "scoped_iam_simulation": True,
        "configuration_fingerprint": "hash", "checked_at": int(time.time())}))
    namespace = checker["enabled_canary"].__globals__
    monkeypatch.setitem(namespace, "PRE_FILE", proof)
    calls = []
    def altered_policy(*args):
        calls.append(args[2]["Role"])
        raise ValueError("Scope changed on the same role")
    monkeypatch.setitem(namespace, "check_iam_scope", altered_policy)
    with pytest.raises(ValueError, match="Scope changed"):
        checker["enabled_canary"](object(), {}, {"Role": "unchanged-arn"}, {}, "origin", "hash")
    assert calls == ["unchanged-arn"]


def test_diagnostics_expose_only_fixed_stage_and_known_error_codes(checker):
    from botocore.exceptions import ClientError, NoCredentialsError, ParamValidationError
    checker["step"]("iam.table_own")
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": "synthetic-private-marker"}}, "SimulatePrincipalPolicy")
    assert checker["diagnostic"](error) == {"failed_check": "iam.table_own", "category": "aws_api_error", "aws_error_code": "AccessDenied"}
    for value in [KeyError("synthetic-private-marker"),
                  ValueError("synthetic-private-marker"),
                  FileNotFoundError("synthetic-private-marker"),
                  ParamValidationError(report="synthetic-private-marker"),
                  ClientError({"Error": {"Code": "synthetic-private-marker", "Message": "synthetic-private-marker"}}, "x")]:
        assert "synthetic-private-marker" not in json.dumps(checker["diagnostic"](value))
    assert checker["diagnostic"](NoCredentialsError())["category"] == "aws_credentials_unavailable"
    with pytest.raises(ValueError):
        checker["step"]("synthetic-private-marker")


def test_diagnostic_failure_preserves_gate_proof_and_redacts_output(checker, monkeypatch, tmp_path, capsys):
    import sys
    from botocore.exceptions import ClientError
    namespace = checker["main"].__globals__
    state, proof, report = tmp_path / "state.json", tmp_path / "proof.json", tmp_path / "diagnostic.json"
    state.write_text("{}")
    proof.write_text('{"passed":false,"prior_evidence":true}')
    monkeypatch.setitem(namespace, "STATE_FILE", state)
    monkeypatch.setitem(namespace, "PRE_FILE", proof)
    monkeypatch.setitem(namespace, "DIAGNOSTIC_FILE", report)
    monkeypatch.setattr(namespace["boto3"], "Session", lambda **kwargs: object())
    def failure(*args):
        checker["step"]("function.secret_binding")
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "synthetic-private-marker"}}, "x")
    monkeypatch.setitem(namespace, "configuration", failure)
    monkeypatch.setattr(sys, "argv", ["checker", "--phase", "pre-enable", "--diagnose"])
    with pytest.raises(SystemExit) as error:
        checker["main"]()
    assert error.value.code == 1 and json.loads(proof.read_text())["prior_evidence"] is True
    output = capsys.readouterr()
    assert "synthetic-private-marker" not in output.out + output.err + report.read_text()
    assert json.loads(report.read_text())["diagnostic"]["failed_check"] == "function.secret_binding"


def test_diagnostic_mode_cannot_run_enabled_canary(checker, monkeypatch, capsys):
    import sys
    namespace = checker["main"].__globals__
    monkeypatch.setattr(sys, "argv", ["checker", "--phase", "enabled-canary", "--diagnose"])
    monkeypatch.setattr(namespace["boto3"], "Session", lambda **kwargs: pytest.fail("No AWS session allowed"))
    with pytest.raises(SystemExit) as error:
        checker["main"]()
    assert error.value.code == 2
