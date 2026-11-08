"""User-run checks only: never enables, deploys, or changes IAM/configuration."""

import argparse
import hashlib
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.exceptions import (
    ConnectTimeoutError, CredentialRetrievalError, EndpointConnectionError, NoCredentialsError,
    ParamValidationError, PartialCredentialsError, ProfileNotFound, ReadTimeoutError,
)
import httpx

from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.oidc_spike_lambda import COOKIE, digest
from verify_oidc_cloud import collect_complete_logs


ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = ROOT / "local/oidc/aws-probe-state.json"
PRE_FILE = ROOT / "local/oidc/aws-runtime-pre-enable.json"
EVIDENCE_FILE = ROOT / "local/oidc/aws-enabled-canary.json"
DIAGNOSTIC_FILE = ROOT / "local/oidc/aws-pre-enable-diagnostic.json"
CURRENT_CHECK = "initialization"
CHECK_NAMES = {
    "initialization", "manifest.read", "aws.session", "sts.identity", "manifest.account_region",
    "manifest.stack_arn", "stack.read", "stack.identity_status", "stack.scope",
    "resource.Function", "resource.ExecutionRole", "resource.Transactions", "resource.ProbeSecrets", "resource.Logs",
    "function.read", "function.identity", "function.state", "function.architecture_tracing",
    "function.environment", "function.enabled_flag", "function.no_plaintext_secrets", "function.url_read",
    "function.url_binding", "function.origin_format", "function.origin_redirect", "function.table_binding",
    "function.secret_binding", "function.log_binding", "function.runtime_limits", "iam.role_read", "iam.role_binding",
    "iam.table_own", "iam.table_foreign", "iam.secret_own", "iam.secret_foreign", "iam.logs_own", "iam.logs_foreign", "iam.admin_denied",
    "proof.write", "enabled.canary",
    "canary.operator_key", "canary.rate", "canary.health", "canary.launch_first", "canary.wrong_state",
    "canary.launch_replace", "canary.old_binding", "canary.denial", "canary.result", "canary.replay",
    "canary.logs", "canary.cleanup",
}


def step(name):
    global CURRENT_CHECK
    if name not in CHECK_NAMES:
        raise ValueError("Unknown diagnostic stage.")
    CURRENT_CHECK = name


def diagnostic(error):
    """Only fixed labels/codes: never stringify exceptions, AWS messages, or input."""
    result = {"failed_check": CURRENT_CHECK, "category": "unexpected_error"}
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        details = response.get("Error", {})
        code = details.get("Code") if isinstance(details, dict) else None
        known = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "ValidationError", "ValidationException",
                 "ResourceNotFoundException", "NoSuchEntity", "NoSuchEntityException", "ExpiredToken", "ExpiredTokenException",
                 "InvalidClientTokenId", "UnrecognizedClientException", "SignatureDoesNotMatch", "Throttling", "ThrottlingException",
                 "TooManyRequestsException", "RequestLimitExceeded", "ServiceFailure", "InternalFailure", "InternalError"}
        result.update(category="aws_api_error", aws_error_code=code if code in known else "unclassified_aws_error")
    elif isinstance(error, (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError)):
        result["category"] = "aws_credentials_unavailable"
    elif isinstance(error, ProfileNotFound):
        result["category"] = "aws_profile_unavailable"
    elif isinstance(error, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)):
        result["category"] = "aws_transport_unavailable"
    elif isinstance(error, ParamValidationError):
        result["category"] = "aws_request_validation"
    elif isinstance(error, KeyError):
        result["category"] = "response_or_manifest_field_missing"
    elif isinstance(error, OSError):
        result["category"] = "local_file_or_connection_error"
    elif isinstance(error, ValueError):
        result["category"] = "check_rejected_or_invalid_format"
    return result


def check(condition, name=None):
    if name:
        step(name)
    if not condition:
        raise ValueError("Runtime check rejected.")


def configuration(session, state, enabled):
    step("sts.identity")
    account = session.client("sts").get_caller_identity()["Account"]
    check(state["account"] == account and state["region"] == "ap-southeast-1", "manifest.account_region")
    check(state["stack_id"].startswith(f"arn:aws:cloudformation:ap-southeast-1:{account}:stack/{state['name']}/"), "manifest.stack_arn")
    step("stack.read")
    cloud = session.client("cloudformation")
    stack = cloud.describe_stacks(StackName=state["stack_id"])["Stacks"][0]
    check(stack["StackId"] == state["stack_id"] and stack["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}, "stack.identity_status")
    check({tag["Key"]: tag["Value"] for tag in stack.get("Tags", [])}.get("Scope") == "SPIKE-01", "stack.scope")
    resources = {}
    for logical in ("Function", "ExecutionRole", "Transactions", "ProbeSecrets", "Logs"):
        step("resource." + logical)
        resources[logical] = cloud.describe_stack_resource(StackName=state["stack_id"], LogicalResourceId=logical)["StackResourceDetail"]["PhysicalResourceId"]
    client = session.client("lambda")
    step("function.read")
    function = client.get_function_configuration(FunctionName=resources["Function"])
    check(function["FunctionArn"] == state["function_arn"], "function.identity")
    check(function.get("State") == "Active" and function.get("LastUpdateStatus") == "Successful", "function.state")
    check(function.get("Architectures") == ["x86_64"] and function.get("TracingConfig", {}).get("Mode") == "PassThrough", "function.architecture_tracing")
    step("function.environment")
    variables = function["Environment"]["Variables"]
    check(variables["SPIKE_ENABLED"] == ("true" if enabled else "false"), "function.enabled_flag")
    check(not {"OIDC_CLIENT_SECRET", "SPIKE_OPERATOR_SECRET", "SPIKE_ENCRYPTION_KEY", "DATABASE_URL", "DATABASE_DIRECT_URL"} & variables.keys(), "function.no_plaintext_secrets")
    step("function.url_read")
    url = client.get_function_url_config(FunctionName=function["FunctionArn"])
    check(url["AuthType"] == "NONE" and url["FunctionUrl"] == state["outputs"]["ProbeUrl"], "function.url_binding")
    origin = url["FunctionUrl"].rstrip("/")
    check(re.fullmatch(r"https://[a-z0-9]+\.lambda-url\.ap-southeast-1\.on\.aws", origin), "function.origin_format")
    check(variables["PUBLIC_ORIGIN"] == origin and variables["OIDC_REDIRECT_URI"] == origin + "/api/v1/auth/callback", "function.origin_redirect")
    check(variables["SPIKE_TABLE_NAME"] == resources["Transactions"] == state["outputs"]["TableName"], "function.table_binding")
    check(variables["SPIKE_SECRET_ARN"] == resources["ProbeSecrets"] == state["outputs"]["SecretArn"], "function.secret_binding")
    check(resources["Logs"] == state["outputs"]["LogGroup"], "function.log_binding")
    check(function["Runtime"] == "python3.13" and function["Timeout"] == 25 and function["MemorySize"] == 256, "function.runtime_limits")
    step("iam.role_read")
    role = session.client("iam").get_role(RoleName=resources["ExecutionRole"])["Role"]
    check(role["Arn"] == function["Role"], "iam.role_binding")
    fingerprint = hashlib.sha256(json.dumps({
        "function": function["FunctionArn"], "role": function["Role"], "code": function["CodeSha256"],
        "variables": {k: v for k, v in variables.items() if k != "SPIKE_ENABLED"},
        "runtime": function["Runtime"], "timeout": function["Timeout"], "memory": function["MemorySize"],
    }, sort_keys=True).encode()).hexdigest()
    return function, variables, origin, fingerprint


def simulate(iam, role, actions, resource, allowed):
    results, marker = [], None
    while True:
        parameters = dict(PolicySourceArn=role, ActionNames=actions, ResourceArns=[resource])
        if marker:
            parameters["Marker"] = marker
        page = iam.simulate_principal_policy(**parameters)
        results.extend(page["EvaluationResults"])
        if not page.get("IsTruncated"):
            break
        marker = page.get("Marker")
        check(bool(marker))
    check(len(results) == len(actions))
    check({result["EvalActionName"].casefold() for result in results} == {action.casefold() for action in actions})
    def decision_matches(value):
        return value == "allowed" if allowed else value in {"implicitDeny", "explicitDeny"}

    for result in results:
        check(not result.get("MissingContextValues") and decision_matches(result["EvalDecision"]))
        # New IAM responses use a resource-type template at the top level.
        # Match the requested ARN against the per-resource result, never a wildcard.
        if "ResourceSpecificResults" in result:
            specific = result["ResourceSpecificResults"]
            check(len(specific) == 1)
            item = specific[0]
            check(item.get("EvalResourceName") == resource and not item.get("MissingContextValues")
                  and decision_matches(item.get("EvalResourceDecision")))
        else:
            # Older responses can carry the exact resource directly.
            check(result.get("EvalResourceName") == resource)


def check_iam_scope(session, state, function, variables):
    iam = session.client("iam")
    account, role = state["account"], function["Role"]
    table = f"arn:aws:dynamodb:ap-southeast-1:{account}:table/{variables['SPIKE_TABLE_NAME']}"
    ddb_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"]
    step("iam.table_own"); simulate(iam, role, ddb_actions, table, True)
    step("iam.table_foreign"); simulate(iam, role, ddb_actions, table + "-forbidden", False)
    step("iam.secret_own"); simulate(iam, role, ["secretsmanager:GetSecretValue"], variables["SPIKE_SECRET_ARN"], True)
    step("iam.secret_foreign"); simulate(iam, role, ["secretsmanager:GetSecretValue"], f"arn:aws:secretsmanager:ap-southeast-1:{account}:secret:eai-probe-forbidden-AbCd12", False)
    # Simulate the deployed group scope. Concrete stream simulation returned deny
    # even for an isolated Resource='*' allow policy; runtime logging is proven
    # separately by the mandatory correlated enabled-canary log checks.
    log_resource = f"arn:aws:logs:ap-southeast-1:{account}:log-group:{state['outputs']['LogGroup']}:*"
    step("iam.logs_own"); simulate(iam, role, ["logs:CreateLogStream", "logs:PutLogEvents"], log_resource, True)
    foreign_log = f"arn:aws:logs:ap-southeast-1:{account}:log-group:{state['outputs']['LogGroup']}-forbidden:*"
    step("iam.logs_foreign"); simulate(iam, role, ["logs:CreateLogStream", "logs:PutLogEvents"], foreign_log, False)
    step("iam.admin_denied"); simulate(iam, role, ["iam:PassRole", "lambda:UpdateFunctionConfiguration"], "*", False)


def pre_enable(session, state, function, variables, fingerprint, *, write_proof=True):
    check_iam_scope(session, state, function, variables)
    result = {"phase": "pre-enable", "passed": True, "checked_at": int(time.time()), "configuration_fingerprint": fingerprint,
                  "scoped_iam_simulation": True, "logging_simulation_scope": "log_group_policy_scope",
                  "actual_runtime_iam": "not_tested", "actual_user_login": "not_tested"}
    if write_proof:
        step("proof.write")
        PRE_FILE.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def check_prerequisite(proof, fingerprint, now):
    check(proof["phase"] == "pre-enable" and proof["passed"] is True and proof["scoped_iam_simulation"] is True)
    check(proof["configuration_fingerprint"] == fingerprint and 0 <= now - proof["checked_at"] <= 3600)


def authorization(response, variables):
    check(response.status_code == 200)
    url = response.json()["authorization_url"]
    parsed, expected = urlsplit(url), urlsplit(variables["OIDC_AUTHORIZATION_ENDPOINT"])
    check((parsed.scheme, parsed.netloc, parsed.path) == (expected.scheme, expected.netloc, expected.path))
    check(not parsed.fragment)
    params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    check(all(len(value) == 1 for value in params.values()))
    for key in ("state", "nonce", "code_challenge"):
        check(bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", params[key][0])))
    check(params["response_type"] == ["code"] and params["code_challenge_method"] == ["S256"])
    check(params["redirect_uri"] == [variables["OIDC_REDIRECT_URI"]] and params["client_id"] == [variables["OIDC_CLIENT_ID"]])
    check(params["scope"] == ["openid profile"] and "code_verifier" not in params)
    return params


def secure_cookie(response):
    headers = response.headers.get_list("set-cookie")
    check(len(headers) == 1)
    cookie = SimpleCookie()
    cookie.load(headers[0])
    check(set(cookie.keys()) == {COOKIE})
    item = cookie[COOKIE]
    check(bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", item.value)))
    check(bool(item["secure"]) and bool(item["httponly"]) and item["samesite"].lower() == "lax")
    check(item["path"] == "/" and item["domain"] == "" and item["max-age"] == "300")
    # Reject duplicate attributes/cookies that parsers could otherwise overwrite.
    fields = [part.strip().split("=", 1)[0].casefold() for part in headers[0].split(";")]
    check(len(fields) == len(set(fields)) and "domain" not in fields)
    return item.value


def require_clean_logs(messages, forbidden):
    check(all(value not in message for message in messages for value in forbidden if value))


def enabled_canary(session, state, function, variables, origin, fingerprint):
    check_prerequisite(json.loads(PRE_FILE.read_text()), fingerprint, int(time.time()))
    # Role ARN alone does not bind mutable policies; simulate the live scope again.
    check_iam_scope(session, state, function, variables)
    step("canary.operator_key")
    operator = (ROOT / "local/oidc/aws-spike-operator.key").read_text().strip()
    check(bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", operator)))
    ddb = session.client("dynamodb")
    table = variables["SPIKE_TABLE_NAME"]
    step("canary.rate")
    rate = ddb.get_item(TableName=table, Key={"pk": {"S": "rate"}}, ConsistentRead=True).get("Item", {})
    if rate and int(rate["window"]["N"]) == int(time.time()) // 60:
        check(int(rate["requests"]["N"]) <= 3)  # Seven operations, without resetting shared counters.
    invocations, forbidden, owned = [], [operator], []
    started = int(time.time() * 1000)
    def record(response, status):
        check(response.status_code == status)
        invocation = response.headers.get("x-amzn-requestid")
        check(bool(invocation))
        invocations.append(invocation)
        check(response.headers.get("cache-control") == "no-store" and response.headers.get("referrer-policy") == "no-referrer")
        if response.headers.get_list("set-cookie"):
            secure_cookie(response)
        return response
    def read(key):
        return ddb.get_item(TableName=table, Key={"pk": {"S": key}}, ConsistentRead=True).get("Item", {})
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=25) as http:
            step("canary.health")
            health = record(http.get(origin + "/health/live"), 200)
            check(health.json() == {"status": "ok", "scope": "oidc_spike_only"})
            headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json", "X-EAI-Spike-Operator": operator}
            step("canary.launch_first")
            launch = record(http.post(origin + "/spike/start", headers=headers, json={}), 200)
            first = authorization(launch, variables)
            first_cookie = http.cookies.get(COOKIE)
            check(secure_cookie(launch) == first_cookie)
            first_key = "tx#" + digest("binding", first_cookie)
            owned.append((first_key, digest("state", first["state"][0])))
            wrong_state, dummy_code = secrets.token_urlsafe(32), "enabled-dummy-" + secrets.token_hex(12)
            forbidden.extend([first_cookie, first["state"][0], first["nonce"][0], wrong_state, dummy_code])
            step("canary.wrong_state")
            record(http.get(origin + "/api/v1/auth/callback", params={"code": dummy_code, "state": wrong_state}), 503)
            row = read(first_key)
            check(row["status"]["S"] == "pending" and "verifier" in row)
            step("canary.launch_replace")
            launch = record(http.post(origin + "/spike/start", headers=headers, json={}), 200)
            second = authorization(launch, variables)
            second_cookie = http.cookies.get(COOKIE)
            check(secure_cookie(launch) == second_cookie)
            check(bool(second_cookie) and second_cookie != first_cookie and not read(first_key))
            second_key = "tx#" + digest("binding", second_cookie)
            owned.append((second_key, digest("state", second["state"][0])))
            forbidden.extend([second_cookie, second["state"][0], second["nonce"][0]])
            step("canary.old_binding")
            record(http.get(origin + "/api/v1/auth/callback", headers={"Cookie": COOKIE + "=" + first_cookie},
                            params={"code": dummy_code, "state": first["state"][0]}), 503)
            # A synthetic denial claims/finalizes without exchanging a code or visiting Cognito.
            denial = {"error": "access_denied", "state": second["state"][0]}
            step("canary.denial")
            callback = record(http.get(origin + "/api/v1/auth/callback", params=denial), 303)
            check(callback.headers.get("location") == "/spike/result")
            row = read(second_key)
            check(row["status"]["S"] == "failed" and "verifier" not in row)
            step("canary.result")
            outcome = record(http.get(origin + "/spike/result"), 200)
            check(outcome.json() == {"provider_verified": False, "application_session_created": False})
            step("canary.replay")
            record(http.get(origin + "/api/v1/auth/callback", params=denial), 503)
        step("canary.logs")
        logs = session.client("logs")
        for invocation in invocations:
            require_clean_logs(collect_complete_logs(logs, state["outputs"]["LogGroup"], started, invocation), forbidden)
        result = {"phase": "enabled-canary", "passed": True, "checked_at": int(time.time()), "configuration_fingerprint": fingerprint,
                  "runtime_secret_read": True, "runtime_store_create_replace_claim_finalize_read": True,
                  "iam_scope_resimulated": True, "secure_binding_cookie": True,
                  "wrong_state_old_binding_replay_denied": True, "verifier_removed": True,
                  "synthetic_denial_no_session": True, "correlated_cloudwatch_canaries_absent": True,
                  "invocations_checked": len(invocations), "actual_user_login": "not_tested"}
        return result
    finally:
        # Operator-side cleanup is limited to this run's exact state-hash-bound rows.
        for key, state_hash in owned:
            row = read(key)
            if row:
                check(row.get("state_hash", {}).get("S") == state_hash)
                ddb.delete_item(TableName=table, Key={"pk": {"S": key}}, ConditionExpression="state_hash=:state",
                                ExpressionAttributeValues={":state": {"S": state_hash}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=["pre-enable", "enabled-canary"])
    parser.add_argument("--diagnose", action="store_true", help="Read-only pre-enable diagnostics; does not write a pass proof.")
    args = parser.parse_args()
    if args.diagnose and args.phase != "pre-enable":
        parser.error("--diagnose is only available with --phase pre-enable")
    safe_transport_logs()
    try:
        step("manifest.read")
        state = json.loads(STATE_FILE.read_text())
        step("aws.session")
        session = boto3.Session(region_name="ap-southeast-1")
        function, variables, origin, fingerprint = configuration(session, state, args.phase == "enabled-canary")
        if args.phase == "pre-enable":
            result = pre_enable(session, state, function, variables, fingerprint, write_proof=not args.diagnose)
        else:
            step("enabled.canary")
            result = enabled_canary(session, state, function, variables, origin, fingerprint)
            EVIDENCE_FILE.write_text(json.dumps(result, indent=2) + "\n")
        if args.diagnose:
            result["diagnostic_only"] = True
            DIAGNOSTIC_FILE.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        failed = {"phase": args.phase, "passed": False, "checked_at": int(time.time()), "actual_user_login": "not_tested"}
        failed["diagnostic"] = diagnostic(error)
        if args.diagnose:
            failed["diagnostic_only"] = True
        try:
            (DIAGNOSTIC_FILE if args.diagnose else PRE_FILE if args.phase == "pre-enable" else EVIDENCE_FILE).write_text(json.dumps(failed) + "\n")
        except OSError:
            pass
        print(json.dumps(failed))
        print("Do not log in. Keep/set SPIKE_ENABLED=false manually and ask Lead to review sanitized output.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
