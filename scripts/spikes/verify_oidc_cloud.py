"""Verify the exact disabled AWS probe, including optional disposable store fixtures."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer
import httpx

from enterprise_ai_platform.infrastructure.oidc_spike_store import DynamoSpikeStore, StoreRejected
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs


ROOT = Path(__file__).resolve().parents[2]


def collect_complete_logs(logs, group, started, invocation, *, pause=time.sleep):
    required = [part + " RequestId: " + invocation for part in ("START", "END", "REPORT")]
    for _ in range(25):
        messages = [event["message"]
                    for page in logs.get_paginator("filter_log_events").paginate(logGroupName=group, startTime=started)
                    for event in page.get("events", [])]
        if all(any(part in message for message in messages) for part in required):
            return messages
        pause(2)
    raise ValueError("Complete correlated invocation logs are not available.")


def verify_store(client, table):
    run = secrets.token_hex(16)
    store = DynamoSpikeStore(table, client=client)
    now, owned = int(time.time()), []
    budget_before = client.get_item(TableName=table, Key={"pk": {"S": "budget"}}, ConsistentRead=True).get("Item")
    if budget_before or client.get_item(TableName=table, Key={"pk": {"S": "rate"}}, ConsistentRead=True).get("Item"):
        raise ValueError("Store fixture check requires unused control rows while probe is disabled.")
    def read(key):
        data = client.get_item(TableName=table, Key={"pk": {"S": key}}, ConsistentRead=True).get("Item", {})
        return {k: TypeDeserializer().deserialize(v) for k, v in data.items()}
    def create(index, expires):
        key = "tx#" + hashlib.sha256((run + str(index)).encode()).hexdigest()
        state = hashlib.sha256(("state" + run + str(index)).encode()).hexdigest()
        store.start({"pk": key, "state_hash": state, "nonce_hash": "a" * 64,
                     "verifier": "synthetic-encrypted-fixture", "expires_at": expires,
                     "status": "pending", "probe_run": run}, None)
        owned.append(key)
        return key, state
    try:
        key, state = create(1, now + 300)
        def claim():
            try:
                store.claim(key, state, now)
                return True
            except StoreRejected:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))
        assert sum(results) == 1 and read(key)["status"] == "consumed" and "verifier" not in read(key)
        assert claim() is False
        store.finish(key, True)
        assert store.result(key, now) is True
        other, other_state = create(2, now + 300)
        expired, expired_state = create(3, now)
        for bad_key, bad_state in [(other, "wrong"), (expired, expired_state), ("tx#missing-" + run, state)]:
            try:
                store.claim(bad_key, bad_state, now)
            except StoreRejected:
                pass
            else:
                raise AssertionError("Invalid store claim accepted")
        assert read(other)["status"] == "pending" and "verifier" in read(other)
        prior = read("budget")["launches"]
        client.update_item(TableName=table, Key={"pk": {"S": "budget"}},
            UpdateExpression="SET launches=:full", ConditionExpression="launches=:prior",
            ExpressionAttributeValues={":full": {"N": "100"}, ":prior": {"N": str(prior)}})
        rejected = "tx#quota-" + run
        try:
            try:
                store.start({"pk": rejected, "probe_run": run}, other)
            except StoreRejected:
                pass
            else:
                raise AssertionError("Launch quota did not reject")
            assert not read(rejected) and read(other)["status"] == "pending"
        finally:
            client.update_item(TableName=table, Key={"pk": {"S": "budget"}},
                UpdateExpression="SET launches=:prior", ConditionExpression="launches=:full",
                ExpressionAttributeValues={":full": {"N": "100"}, ":prior": {"N": str(prior)}})
        client.put_item(TableName=table, Item={"pk": {"S": "rate"}, "probe_run": {"S": run},
            "window": {"N": str(now // 60 - 1)}, "requests": {"N": "10"}}, ConditionExpression="attribute_not_exists(pk)")
        for _ in range(10):
            store.limit(now)
        try:
            store.limit(now)
        except StoreRejected:
            pass
        else:
            raise AssertionError("Rate quota did not reject")
        assert read("rate")["requests"] == 10
        return {"parallel_claim_one_winner": True, "verifier_removed": True, "replay_denied": True,
                "wrong_state_missing_binding_expiry_denied": True, "result_bound_to_key": True,
                "quota_100_atomic_rollback": True, "rate_10_and_rollover": True}
    finally:
        for key in owned:
            client.delete_item(TableName=table, Key={"pk": {"S": key}}, ConditionExpression="probe_run=:run",
                               ExpressionAttributeValues={":run": {"S": run}})
        if read("rate").get("probe_run") == run:
            client.delete_item(TableName=table, Key={"pk": {"S": "rate"}}, ConditionExpression="probe_run=:run",
                               ExpressionAttributeValues={":run": {"S": run}})


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-store", action="store_true", help="Use exact scoped fictitious rows while function is disabled.")
    args = parser.parse_args()
    safe_transport_logs()
    try:
        state = json.loads((ROOT / "local/oidc/aws-probe-state.json").read_text())
        session = boto3.Session(region_name="ap-southeast-1")
        account = session.client("sts").get_caller_identity()["Account"]
        assert state["account"] == account and state["stage"] == "configured_disabled"
        cloud = session.client("cloudformation")
        stack = cloud.describe_stacks(StackName=state["stack_id"])["Stacks"][0]
        assert stack["StackId"] == state["stack_id"] and stack["StackStatus"] == "CREATE_COMPLETE"
        client = session.client("lambda")
        function = client.get_function_configuration(FunctionName=state["function_arn"])
        variables = function["Environment"]["Variables"]
        assert variables["SPIKE_ENABLED"] == "false" and function["Runtime"] == "python3.13"
        assert variables["SPIKE_SECRET_ARN"] == state["outputs"]["SecretArn"]
        assert not {"OIDC_CLIENT_SECRET", "SPIKE_OPERATOR_SECRET", "SPIKE_ENCRYPTION_KEY", "DATABASE_URL", "DATABASE_DIRECT_URL"} & variables.keys()
        concurrency = client.get_function_concurrency(FunctionName=state["function_arn"])
        assert (concurrency.get("ReservedConcurrentExecutions") == 2 if state["concurrency_mode"] == "reserved"
                else "ReservedConcurrentExecutions" not in concurrency)
        started, marker = int(time.time() * 1000), "aws-spike-canary-" + secrets.token_hex(12)
        url = state["outputs"]["ProbeUrl"].rstrip("/")
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as http:
            result = http.get(url + "/api/v1/auth/callback", params={"code": marker, "state": "synthetic-not-a-login"})
            assert result.status_code == 404 and result.json() == {"status": "not_found"}
            invocation = result.headers.get("x-amzn-requestid")
            assert invocation
        logs = session.client("logs")
        messages = collect_complete_logs(logs, state["outputs"]["LogGroup"], started, invocation)
        assert all(marker not in message and "synthetic-not-a-login" not in message for message in messages)
        evidence = {"linux_import_and_disabled_http": True, "no_plaintext_secret_environment": True,
                    "concurrency_mode_verified": state["concurrency_mode"], "disabled_canary_query_not_in_cloudwatch": True,
                    "log_event_count": len(messages), "actual_user_login": "not_tested"}
        if args.live_store:
            evidence["live_dynamodb"] = verify_store(session.client("dynamodb"), state["outputs"]["TableName"])
        (ROOT / "local/oidc/aws-disabled-verification.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, sort_keys=True))
    except Exception:
        print("AWS probe verification failed. Inspect only sanitized diagnostics locally; login remains disabled.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
