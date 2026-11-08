"""Offline recovery safeguards; no account or password from the real environment."""

from pathlib import Path
import runpy

import pytest


@pytest.fixture
def helper(monkeypatch):
    scripts = Path(__file__).resolve().parents[3] / "scripts/spikes"
    monkeypatch.syspath_prepend(str(scripts))
    return runpy.run_path(str(scripts / "set_cognito_test_password.py"))


@pytest.mark.parametrize("pages", [[], [{"Users": []}],
    [{"Users": [{"Username": "test", "Enabled": True, "UserStatus": "CONFIRMED"}]}],
    [{"Users": [{"Username": "test", "Enabled": False, "UserStatus": "FORCE_CHANGE_PASSWORD"}]}],
    [{"Users": [{"Username": "first", "Enabled": True, "UserStatus": "FORCE_CHANGE_PASSWORD"}]},
     {"Users": [{"Username": "second", "Enabled": True, "UserStatus": "FORCE_CHANGE_PASSWORD"}]}],
])
def test_recovery_refuses_ambiguous_or_ineligible_accounts(helper, pages):
    class Client:
        def get_paginator(self, name):
            return self
        def paginate(self, **kwargs):
            return pages
    with pytest.raises(ValueError):
        helper["select_test_user"](Client(), "pool")


@pytest.mark.parametrize("confirmation,passwords", [("NO", []), ("SET", ["one", "two"])])
def test_cancel_or_password_mismatch_never_calls_aws(helper, confirmation, passwords):
    inputs = iter(passwords)
    with pytest.raises(ValueError):
        helper["recover"](object(), "pool", "test", confirm=lambda _: confirmation, hidden=lambda _: next(inputs))


def test_echo_fallback_is_rejected_without_mutation(helper):
    import getpass
    import warnings
    def unsafe_prompt(_):
        warnings.warn("private-marker", getpass.GetPassWarning)
        pytest.fail("Fallback echo must not be reached")
    with pytest.raises(getpass.GetPassWarning):
        helper["recover"](object(), "pool", "test", confirm=lambda _: "SET", hidden=unsafe_prompt)


def test_account_state_change_blocks_mutation(helper):
    class Client:
        def admin_get_user(self, **kwargs):
            return {"Enabled": True, "UserStatus": "CONFIRMED"}
        def admin_set_user_password(self, **kwargs):
            pytest.fail("An already confirmed account must not be changed")
    with pytest.raises(ValueError):
        helper["recover"](Client(), "pool", "test", confirm=lambda _: "SET", hidden=lambda _: "Synthetic!Password1")


def test_permanent_password_recovery_uses_exact_account_and_checks_status(helper):
    import boto3
    from botocore.stub import Stubber
    client = boto3.client("cognito-idp", region_name="ap-southeast-1",
                          aws_access_key_id="synthetic", aws_secret_access_key="synthetic")
    user = {"Username": "test", "Enabled": True, "UserStatus": "FORCE_CHANGE_PASSWORD"}
    account = {"UserPoolId": "ap-southeast-1_Test", "Username": "test"}
    with Stubber(client) as stub:
        stub.add_response("admin_get_user", user, account)
        stub.add_response("admin_set_user_password", {}, {**account, "Password": "Synthetic!Password1", "Permanent": True})
        stub.add_response("admin_get_user", {**user, "UserStatus": "CONFIRMED"}, account)
        result = helper["recover"](client, account["UserPoolId"], "test", confirm=lambda _: "SET", hidden=lambda _: "Synthetic!Password1")
        assert result["account_status"] == "CONFIRMED" and result["actual_oidc_login"] == "not_tested"
        assert "Synthetic!Password1" not in str(result)
        stub.assert_no_pending_responses()
    client.close()


@pytest.mark.parametrize("mismatch", ["issuer", "client", "stack", "issuer_host"])
def test_target_drift_stops_before_prompt_or_cognito_access(helper, monkeypatch, tmp_path, mismatch, capsys):
    import json
    import sys
    namespace = helper["main"].__globals__
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"stack_id": "exact-stack"}))
    issuer = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test"
    values = {"OIDC_ISSUER": issuer, "OIDC_CLIENT_ID": "original-client"}
    stack = {"StackId": "exact-stack", "Parameters": [{"ParameterKey": "Issuer", "ParameterValue": issuer},
             {"ParameterKey": "ClientId", "ParameterValue": "original-client"}]}
    if mismatch == "issuer":
        values["OIDC_ISSUER"] += "Other"
    elif mismatch == "client":
        values["OIDC_CLIENT_ID"] = "other-client"
    elif mismatch == "stack":
        stack["StackId"] = "other-stack"
    else:
        values["OIDC_ISSUER"] = "https://private-marker/ap-southeast-1_Test"
        stack["Parameters"][0]["ParameterValue"] = values["OIDC_ISSUER"]
    class Session:
        def client(self, service):
            assert service == "cloudformation", "Cognito must not be reached"
            return self
        def describe_stacks(self, **kwargs):
            return {"Stacks": [stack]}
    monkeypatch.setitem(namespace, "STATE_FILE", state_file)
    monkeypatch.setitem(namespace, "configuration", lambda *args: ({}, values, "", ""))
    monkeypatch.setitem(namespace, "recover", lambda *args: pytest.fail("Password prompt must not be reached"))
    monkeypatch.setattr(namespace["boto3"], "Session", lambda **kwargs: Session())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "argv", ["helper", "--set-permanent-password"])
    with pytest.raises(SystemExit) as failure:
        helper["main"]()
    assert failure.value.code == 1
    assert "private-marker" not in capsys.readouterr().out
