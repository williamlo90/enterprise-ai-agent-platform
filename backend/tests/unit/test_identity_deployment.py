"""Offline deployment guards: exact execution role, foreign denials and auto-rollback."""

from copy import deepcopy
import json
from pathlib import Path
import runpy

import pytest


@pytest.fixture
def deploy(monkeypatch,tmp_path):
    scripts=Path(__file__).parents[3]/"scripts/spikes"
    monkeypatch.syspath_prepend(str(scripts))
    module=runpy.run_path(str(scripts/"deploy_identity_api.py"))
    record=tmp_path/"deployment.json"
    record.write_text(json.dumps({"new_code_sha256":"new-hash","old_role_arn":"expected-role"}))
    module["identity_status"].__globals__["RECORD"]=record
    return module


def setup_status(*,role="expected-role",broad_secret=False,broad_logs=False):
    origin="https://fixture.lambda-url.ap-southeast-1.on.aws"
    state={"account":"111122223333","stack_id":"exact-stack","function_arn":"exact-function",
        "outputs":{"ProbeUrl":origin+"/","SecretArn":"own-secret","TableName":"own-table","LogGroup":"own-group"}}
    function={"FunctionArn":"exact-function","Handler":"enterprise_ai_platform.lambda_api.handler","Role":role,
        "State":"Active","LastUpdateStatus":"Successful","Timeout":60,"CodeSha256":"new-hash",
        "Runtime":"python3.13","Architectures":["x86_64"],"MemorySize":256,"TracingConfig":{"Mode":"PassThrough"},
        "Environment":{"Variables":{"APP_AUTH_ENABLED":"false","SPIKE_ENABLED":"false","PUBLIC_ORIGIN":origin,
            "OIDC_REDIRECT_URI":origin+"/api/v1/auth/callback","SPIKE_SECRET_ARN":"own-secret",
            "OIDC_ISSUER":"issuer","OIDC_CLIENT_ID":"client","OIDC_AUTHORIZATION_ENDPOINT":"authorize",
            "OIDC_TOKEN_ENDPOINT":"token","OIDC_JWKS_URI":"jwks"}}}
    class Client:
        def __init__(self):self.simulations=[]
        def get_caller_identity(self):return {"Account":state["account"]}
        def describe_stacks(self,**kwargs):return {"Stacks":[{"StackId":"exact-stack","StackStatus":"UPDATE_COMPLETE","Tags":[{"Key":"Scope","Value":"SPIKE-01"}],
            "Parameters":[{"ParameterKey":k,"ParameterValue":v} for k,v in {"ApplicationMode":"identity","Issuer":"issuer","ClientId":"client",
                "AuthorizationEndpoint":"authorize","TokenEndpoint":"token","JwksUri":"jwks","PublicOrigin":origin,
                "RedirectUri":origin+"/api/v1/auth/callback"}.items()]}]}
        def describe_stack_resource(self,**kwargs):return {"StackResourceDetail":{"PhysicalResourceId":"fn" if kwargs["LogicalResourceId"]=="Function" else "expected-role-name"}}
        def get_function_configuration(self,**kwargs):return deepcopy(function)
        def get_role(self,**kwargs):return {"Role":{"Arn":"expected-role"}}
        def get_function_url_config(self,**kwargs):return {"AuthType":"NONE","FunctionUrl":origin+"/"}
        def simulate_principal_policy(self,**kwargs):
            self.simulations.append(kwargs)
            resource=kwargs["ResourceArns"][0]
            allowed=(resource=="own-secret" or resource.startswith("arn:aws:logs:") and "forbidden" not in resource)
            if broad_secret and resource=="own-secret-forbidden":allowed=True
            if broad_logs and resource.startswith("arn:aws:logs:"):allowed=True
            return {"EvaluationResults":[{"EvalActionName":action,"EvalResourceName":resource,
                    "EvalDecision":"allowed" if allowed else "implicitDeny"} for action in kwargs["ActionNames"]]}
        def client(self,name):return self
    return Client(),state,function


def test_role_drift_is_rejected_before_simulation(deploy):
    session,state,_=setup_status(role="other-role")
    with pytest.raises(ValueError):deploy["identity_status"](session,state,enabled=False)
    assert session.simulations==[]


@pytest.mark.parametrize("broaden",["secret","logs"])
def test_foreign_permission_broadening_blocks_enable(deploy,broaden):
    session,state,_=setup_status(broad_secret=broaden=="secret",broad_logs=broaden=="logs")
    with pytest.raises(ValueError):deploy["identity_status"](session,state,enabled=False)
    assert any("forbidden" in c["ResourceArns"][0] for c in session.simulations)


def test_expected_role_and_own_only_permissions_pass(deploy):
    session,state,function=setup_status()
    assert deploy["identity_status"](session,state,enabled=False)==function
    assert len(session.simulations)==6


def test_auto_rollback_restores_secret_pointer_and_disabled_environment_without_stack_update(deploy):
    state={"account":"111122223333","stack_id":"exact-stack","function_arn":"exact-function","outputs":{"SecretArn":"own-secret"}}
    record={"stack_id":"exact-stack","function_arn":"exact-function","old_role_arn":"expected-role",
        "old_handler":"old-handler","old_code_sha256":"old-code","old_secret_version":"old-version","new_secret_version":"new-version",
        "old_environment":{"SPIKE_ENABLED":"false","PUBLIC_ORIGIN":"https://fixture.example.test"},"old_timeout":25}
    class Client:
        def __init__(self):self.environment_changes=[];self.secret_changes=[]
        def client(self,name):return self
        def get_caller_identity(self):return {"Account":state["account"]}
        def describe_stacks(self,**kwargs):return {"Stacks":[{"StackId":"exact-stack","StackStatus":"UPDATE_ROLLBACK_COMPLETE"}]}
        def get_function_configuration(self,**kwargs):
            return {"FunctionArn":"exact-function","Role":"expected-role","Handler":"old-handler","CodeSha256":"old-code",
                    "Environment":{"Variables":{"APP_AUTH_ENABLED":"false","SPIKE_ENABLED":"false"}},"RevisionId":"revision"}
        def update_function_configuration(self,**kwargs):self.environment_changes.append(kwargs)
        def get_waiter(self,name):return self
        def wait(self,**kwargs):pass
        def update_stack(self,**kwargs):pytest.fail("Auto-rollback must not issue a redundant update")
        def describe_secret(self,**kwargs):return {"VersionIdsToStages":{"old-version":["AWSPREVIOUS"],"new-version":["AWSCURRENT"]}}
        def update_secret_version_stage(self,**kwargs):self.secret_changes.append(kwargs)
        def get_secret_value(self,**kwargs):pytest.fail("Rollback must not read or export secret values")
    client=Client();deploy["rollback"](client,state,record)
    assert len(client.secret_changes)==1 and client.secret_changes[0]["MoveToVersionId"]=="old-version"
    variables=client.environment_changes[-1]["Environment"]["Variables"]
    assert variables["APP_AUTH_ENABLED"]==variables["SPIKE_ENABLED"]=="false"
    assert record["phase"]=="rollback_restoring_disabled_environment"


@pytest.fixture
def canary(monkeypatch,tmp_path):
    scripts=Path(__file__).parents[3]/"scripts/spikes";monkeypatch.syspath_prepend(str(scripts))
    module=runpy.run_path(str(scripts/"check_identity_cloud.py"))
    monkeypatch.setitem(module["main"].__globals__,"ROOT",tmp_path)
    import sys
    monkeypatch.setattr(sys,"argv",["checker","disabled"])
    return module,tmp_path


def test_invalidation_write_failure_stops_before_cloud_without_raw_traceback(canary,monkeypatch,capsys):
    checker,root=canary
    monkeypatch.setitem(checker["main"].__globals__,"check",lambda *_:pytest.fail("Cloud must not be called"))
    def fail(*args,**kwargs):raise OSError("private-marker")
    monkeypatch.setattr(Path,"write_text",fail)
    with pytest.raises(SystemExit) as error:checker["main"]()
    assert error.value.code==1 and error.value.__suppress_context__
    output=capsys.readouterr()
    assert "private-marker" not in output.out+output.err and '"cloud_called": false' in output.out


def test_late_write_failure_cannot_restore_a_stale_pass_or_leak_operation_error(canary,monkeypatch,capsys):
    checker,root=canary
    proof=root/"local/auth02/cloud-disabled-canary.json";proof.parent.mkdir(parents=True)
    proof.write_text('{"passed":true}')
    original=Path.write_text
    calls=[]
    def write(path,*args,**kwargs):
        calls.append(True)
        if len(calls)>1:raise OSError("private-write-marker")
        return original(path,*args,**kwargs)
    def fail_check(*args):raise RuntimeError("private-operation-marker")
    monkeypatch.setattr(Path,"write_text",write)
    monkeypatch.setitem(checker["main"].__globals__,"check",fail_check)
    with pytest.raises(SystemExit) as error:checker["main"]()
    assert error.value.code==1 and error.value.__suppress_context__
    output=capsys.readouterr()
    assert "private-" not in output.out+output.err
    assert json.loads(proof.read_text())["passed"] is False


def test_canary_health_uses_health_contract_without_identity_headers(canary):
    import httpx
    checker,_=canary
    response=httpx.Response(200,json={"status":"ready","scope":"bootstrap"},headers={"x-eai-invocation-id":"fixture"})
    assert checker["validate_response"](response,200,"health_ready")=="fixture"
    with pytest.raises(ValueError):checker["validate_response"](response,200,"me")
    with pytest.raises(ValueError):checker["validate_response"](httpx.Response(200,json={"status":"wrong"},headers={"x-eai-invocation-id":"fixture"}),200,"health_ready")
