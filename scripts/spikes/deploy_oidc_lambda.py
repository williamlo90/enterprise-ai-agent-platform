"""Deploy the isolated probe only after explicit cloud resource/budget approval."""

import argparse
import json
from pathlib import Path
import re
import secrets
from uuid import uuid4
from zipfile import ZipFile

import boto3
from botocore.config import Config
from cryptography.fernet import Fernet

from enterprise_ai_platform.config import load_oidc_probe_settings
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.modules.identity.domain import CognitoProvider


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "local/oidc/aws-probe-state.json"


def save(state):
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--approve-provision", action="store_true",
                        help="Explicit approval is required for AWS resources, IAM and usage charges.")
    parser.add_argument("--resume-rolled-back", action="store_true",
                        help="Reconcile and replace only the exact ROLLBACK_COMPLETE stack in the local manifest.")
    args = parser.parse_args()
    if not args.approve_provision:
        parser.error("Review infra/aws/oidc_spike.yaml and approve resource scope/budget before provisioning.")
    safe_transport_logs()
    try:
        if STATE.exists() and not args.resume_rolled_back:
            raise ValueError("Existing resource manifest must be reconciled first.")
        settings = load_oidc_probe_settings(args.env_file)
        values = settings.model_dump()
        values["client_secret"] = settings.client_secret.get_secret_value()
        provider = CognitoProvider(**values)
        with ZipFile(args.artifact) as package:
            for source in (ROOT / "backend/src/enterprise_ai_platform").rglob("*.py"):
                if "__pycache__" not in source.parts:
                    member = "enterprise_ai_platform/" + source.relative_to(ROOT / "backend/src/enterprise_ai_platform").as_posix()
                    if package.read(member) != source.read_bytes():
                        raise ValueError("Rebuild artifact after source changes.")
        session = boto3.Session(region_name="ap-southeast-1")
        config = Config(connect_timeout=3, read_timeout=15, retries={"total_max_attempts": 2})
        account = session.client("sts", config=config).get_caller_identity()["Account"]
        cloud = session.client("cloudformation", config=config)
        s3 = session.client("s3", config=config)
        if args.resume_rolled_back:
            state = json.loads(STATE.read_text(encoding="utf-8"))
            if (state["account"] != account or state["region"] != "ap-southeast-1"
                    or state["bucket"] != state["name"] or state["artifact_key"] != "oidc-spike.zip"
                    or not re.fullmatch(r"eai-oidc-spike-[a-f0-9]{12}", state["name"])
                    or not state["stack_id"].startswith(f"arn:aws:cloudformation:ap-southeast-1:{account}:stack/{state['name']}/")):
                raise ValueError("Resource manifest binding mismatch.")
            previous = cloud.describe_stacks(StackName=state["stack_id"])["Stacks"][0]
            if (previous["StackStatus"] != "ROLLBACK_COMPLETE" or previous["StackName"] != state["name"]
                    or previous["StackId"] != state["stack_id"]
                    or {tag["Key"]: tag["Value"] for tag in previous.get("Tags", [])}.get("Scope") != "SPIKE-01"):
                raise ValueError("Only the recorded rolled-back stack may be replaced.")
            s3.head_bucket(Bucket=state["bucket"], ExpectedBucketOwner=account)
            old_id = state["stack_id"]
            cloud.delete_stack(StackName=old_id)
            cloud.get_waiter("stack_delete_complete").wait(StackName=old_id, WaiterConfig={"Delay": 3, "MaxAttempts": 100})
            state.setdefault("previous_stack_ids", []).append(old_id)
            state["stage"] = "rolled_back_stack_removed"; save(state)
            name = state["name"]
            print("Recorded rolled-back stack removed; reusing its existing private artifact bucket.", flush=True)
        else:
            name = "eai-oidc-spike-" + uuid4().hex[:12]
            state = {"name": name, "account": account, "region": "ap-southeast-1", "bucket": name,
                     "artifact_key": "oidc-spike.zip", "stage": "planned"}
            STATE.parent.mkdir(parents=True, exist_ok=True)
            save(state)
            s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"})
        STATE.parent.mkdir(parents=True, exist_ok=True)
        state["stage"] = "bucket_created"; save(state)
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        s3.put_bucket_policy(Bucket=name, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Deny", "Principal": "*", "Action": "s3:*", "Resource": [f"arn:aws:s3:::{name}", f"arn:aws:s3:::{name}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
        with args.artifact.open("rb") as body:
            s3.put_object(Bucket=name, Key=state["artifact_key"], Body=body, ServerSideEncryption="AES256")
        limits = session.client("lambda", config=config).get_account_settings()["AccountLimit"]
        # Small/new accounts cannot reserve two while retaining AWS's unreserved floor.
        mode = "reserved" if limits["UnreservedConcurrentExecutions"] >= 102 else "account"
        state["concurrency_mode"] = mode
        state["account_concurrent_limit"] = limits["ConcurrentExecutions"]
        save(state)
        parameters = {"ArtifactBucket": name, "ArtifactKey": state["artifact_key"], "ProbeName": name,
                      "Issuer": provider.issuer, "ClientId": provider.client_id,
                      "AuthorizationEndpoint": provider.authorization_endpoint,
                      "TokenEndpoint": provider.token_endpoint, "JwksUri": provider.jwks_uri, "ConcurrencyMode": mode}
        created = cloud.create_stack(StackName=name, TemplateBody=(ROOT / "infra/aws/oidc_spike.yaml").read_text(),
            Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
            Capabilities=["CAPABILITY_IAM"], Tags=[{"Key": "Scope", "Value": "SPIKE-01"}])
        state["stack_id"] = created["StackId"]; state["stage"] = "stack_creating"; save(state)
        print("AWS probe stack creating; resources are isolated and login remains disabled.", flush=True)
        cloud.get_waiter("stack_create_complete").wait(StackName=state["stack_id"], WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        outputs = cloud.describe_stacks(StackName=state["stack_id"])["Stacks"][0]["Outputs"]
        state["outputs"] = {item["OutputKey"]: item["OutputValue"] for item in outputs}
        state["stage"] = "stack_created_disabled"; save(state)
        operator = secrets.token_urlsafe(32)
        private = {"client_secret": provider.client_secret, "operator_secret": operator,
                   "encryption_key": Fernet.generate_key().decode()}
        session.client("secretsmanager", config=config).put_secret_value(
            SecretId=state["outputs"]["SecretArn"], SecretString=json.dumps(private))
        operator_file = ROOT / "local/oidc/aws-spike-operator.key"
        with operator_file.open("x", encoding="utf-8") as file:
            file.write(operator + "\n")
        origin = state["outputs"]["ProbeUrl"].rstrip("/")
        client = session.client("lambda", config=config)
        current = client.get_function_configuration(FunctionName=state["outputs"]["FunctionName"])
        environment = current["Environment"]["Variables"]
        environment.update(PUBLIC_ORIGIN=origin, OIDC_REDIRECT_URI=origin + "/api/v1/auth/callback", SPIKE_ENABLED="false")
        client.update_function_configuration(FunctionName=current["FunctionArn"], RevisionId=current["RevisionId"],
                                              Environment={"Variables": environment})
        client.get_waiter("function_updated_v2").wait(FunctionName=current["FunctionArn"])
        state["function_arn"] = current["FunctionArn"]
        state["stage"] = "configured_disabled"; save(state)
        print("AWS probe prepared, login disabled. Callback URL:", origin + "/api/v1/auth/callback")
        print("Operator key saved locally; no secret printed. Next: logging canary, register callback, then enable.")
    except Exception:
        print("AWS probe provisioning stopped. Reconcile the local resource manifest; do not rerun blindly.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
