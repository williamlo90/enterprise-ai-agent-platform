"""User-operated recovery for the sole initial-password test account; never run unattended."""

import argparse
from contextlib import closing
import getpass
import hmac
import json
import re
import sys
import warnings

import boto3
from botocore.config import Config

from check_oidc_runtime import STATE_FILE, configuration, safe_transport_logs


def require(condition):
    if not condition:
        raise ValueError("Recovery precondition rejected.")


def select_test_user(client, pool):
    users = []
    for page in client.get_paginator("list_users").paginate(UserPoolId=pool):
        users.extend(page.get("Users", []))
        require(len(users) <= 1)
    require(len(users) == 1)
    user = users[0]
    require(user.get("Enabled") is True and user.get("UserStatus") == "FORCE_CHANGE_PASSWORD")
    require(isinstance(user.get("Username"), str) and bool(user["Username"]))
    return user["Username"]


def bound_pool(stack, state, variables):
    require(stack.get("StackId") == state["stack_id"])
    parameters = {x["ParameterKey"]: x["ParameterValue"] for x in stack.get("Parameters", [])}
    require(parameters.get("Issuer") == variables["OIDC_ISSUER"]
            and parameters.get("ClientId") == variables["OIDC_CLIENT_ID"])
    match = re.fullmatch(r"https://cognito-idp\.ap-southeast-1\.amazonaws\.com/(ap-southeast-1_[A-Za-z0-9]+)",
                         variables["OIDC_ISSUER"])
    require(match is not None)
    return match[1]


def recover(client, pool, username, *, confirm=input, hidden=getpass.getpass):
    require(confirm("Ketik SET untuk menetapkan password permanen akun penguji: ") == "SET")
    # Turn getpass's fallback-to-echo warning into an exception before any password input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = hidden("Password permanen baru (tersembunyi): ")
        repeated = hidden("Ulangi password baru (tersembunyi): ")
    require(bool(password) and len(password) <= 256 and not any(x.isspace() for x in password))
    require(hmac.compare_digest(password.encode(), repeated.encode()))
    # Recheck the selected account immediately before this sole mutation.
    user = client.admin_get_user(UserPoolId=pool, Username=username)
    require(user.get("Enabled") is True and user.get("UserStatus") == "FORCE_CHANGE_PASSWORD")
    client.admin_set_user_password(UserPoolId=pool, Username=username, Password=password, Permanent=True)
    password = repeated = None
    user = client.admin_get_user(UserPoolId=pool, Username=username)
    require(user.get("UserStatus") == "CONFIRMED")
    return {"recovery": "test_account_initial_password", "passed": True, "account_status": "CONFIRMED",
            "email_verification_changed": False, "actual_oidc_login": "not_tested"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-permanent-password", action="store_true")
    args = parser.parse_args()
    if not args.set_permanent_password:
        parser.error("Explicit --set-permanent-password and interactive confirmation are required.")
    safe_transport_logs()
    try:
        require(sys.stdin.isatty())
        state = json.loads(STATE_FILE.read_text())
        session = boto3.Session(region_name="ap-southeast-1")
        _, variables, _, _ = configuration(session, state, True)
        stack = session.client("cloudformation").describe_stacks(StackName=state["stack_id"])["Stacks"][0]
        pool = bound_pool(stack, state, variables)
        with closing(session.client("cognito-idp", config=Config(
                connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}))) as client:
            app = client.describe_user_pool_client(UserPoolId=pool, ClientId=variables["OIDC_CLIENT_ID"])["UserPoolClient"]
            require(app["UserPoolId"] == pool and app["ClientId"] == variables["OIDC_CLIENT_ID"])
            username = select_test_user(client, pool)
            print("Satu akun penguji aktif masih memerlukan perubahan password awal.")
            print("Gunakan password baru sesuai kebijakan pool; password tidak disimpan atau dicetak.")
            print(json.dumps(recover(client, pool, username), sort_keys=True))
    except (Exception, KeyboardInterrupt) as error:
        details = getattr(error, "response", {}).get("Error", {})
        code = details.get("Code")
        allowed = {"InvalidPasswordException", "PasswordHistoryPolicyViolationException", "TooManyRequestsException",
                   "AccessDeniedException", "NotAuthorizedException", "UserNotFoundException"}
        print(json.dumps({"passed": False, "reason": code if code in allowed else "recovery_not_completed"}))
        print("Jangan ulang otomatis. Lead perlu memeriksa status akun sebelum langkah berikutnya.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
