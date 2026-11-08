"""Repurpose the existing isolated AWS host; no new resource or runtime privilege expansion."""

import argparse
import asyncio
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import time
from zipfile import ZipFile

import boto3
from cryptography.fernet import Fernet

from check_oidc_runtime import ROOT, STATE_FILE, configuration, safe_transport_logs, simulate
from enterprise_ai_platform.config import DatabaseProbeSettings, load_settings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate


RECORD=ROOT/"local/auth02/aws-deployment.json"
TEMPLATE=ROOT/"infra/aws/oidc_spike.yaml"


def require(value):
    if not value:raise ValueError("Deployment binding rejected.")


def configuration_fingerprint(function):
    values={k:function.get(k) for k in ("FunctionArn","Handler","Role","CodeSha256","Runtime","Architectures","MemorySize","Timeout","TracingConfig")}
    values["environment"]={k:v for k,v in function["Environment"]["Variables"].items() if k!="APP_AUTH_ENABLED"}
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


def identity_status(session,state,*,enabled,check_iam=True):
    require(session.client("sts").get_caller_identity()["Account"]==state["account"])
    cloud=session.client("cloudformation")
    stack=cloud.describe_stacks(StackName=state["stack_id"])["Stacks"][0]
    require(stack["StackId"]==state["stack_id"] and stack["StackStatus"] in {"CREATE_COMPLETE","UPDATE_COMPLETE"})
    require(any(t["Key"]=="Scope" and t["Value"]=="SPIKE-01" for t in stack.get("Tags",[])))
    client=session.client("lambda")
    resource=cloud.describe_stack_resource(StackName=state["stack_id"],LogicalResourceId="Function")["StackResourceDetail"]["PhysicalResourceId"]
    function=client.get_function_configuration(FunctionName=resource)
    require(function["FunctionArn"]==state["function_arn"] and function["Handler"]=="enterprise_ai_platform.lambda_api.handler")
    require(function["State"]=="Active" and function["LastUpdateStatus"]=="Successful" and function["Timeout"]==60)
    require(function["Runtime"]=="python3.13" and function["Architectures"]==["x86_64"]
        and function["MemorySize"]==256 and function.get("TracingConfig",{}).get("Mode")=="PassThrough")
    variables=function["Environment"]["Variables"]
    iam=session.client("iam")
    role_name=cloud.describe_stack_resource(StackName=state["stack_id"],LogicalResourceId="ExecutionRole")["StackResourceDetail"]["PhysicalResourceId"]
    role=iam.get_role(RoleName=role_name)["Role"]["Arn"]
    require(function["Role"]==role)
    require(variables["APP_AUTH_ENABLED"]==("true" if enabled else "false") and variables["SPIKE_ENABLED"]=="false")
    require(not {"DATABASE_URL","DATABASE_DIRECT_URL","OIDC_CLIENT_SECRET","APP_AUTH_VERIFIER_KEY"}&variables.keys())
    url=client.get_function_url_config(FunctionName=function["FunctionArn"])
    require(url["AuthType"]=="NONE" and url["FunctionUrl"]==state["outputs"]["ProbeUrl"])
    origin=url["FunctionUrl"].rstrip("/")
    require(variables["PUBLIC_ORIGIN"]==origin and variables["OIDC_REDIRECT_URI"]==origin+"/api/v1/auth/callback")
    require(variables["SPIKE_SECRET_ARN"]==state["outputs"]["SecretArn"])
    parameters={p["ParameterKey"]:p["ParameterValue"] for p in stack.get("Parameters",[])}
    require(parameters.get("ApplicationMode")=="identity")
    for parameter,key in (("Issuer","OIDC_ISSUER"),("ClientId","OIDC_CLIENT_ID"),
            ("AuthorizationEndpoint","OIDC_AUTHORIZATION_ENDPOINT"),("TokenEndpoint","OIDC_TOKEN_ENDPOINT"),
            ("JwksUri","OIDC_JWKS_URI"),("PublicOrigin","PUBLIC_ORIGIN"),("RedirectUri","OIDC_REDIRECT_URI")):
        require(parameters.get(parameter)==variables[key])
    if RECORD.exists():
        record=json.loads(RECORD.read_text(encoding="utf-8"))
        require(function["CodeSha256"]==record["new_code_sha256"])
        require(function["Role"]==record["old_role_arn"])
    if not check_iam:return function
    own_log=f"arn:aws:logs:ap-southeast-1:{state['account']}:log-group:{state['outputs']['LogGroup']}:*"
    simulate(iam,role,["logs:CreateLogStream","logs:PutLogEvents"],own_log,True)
    simulate(iam,role,["logs:CreateLogStream","logs:PutLogEvents"],own_log.removesuffix(":*")+"-forbidden:*",False)
    simulate(iam,role,["secretsmanager:GetSecretValue"],variables["SPIKE_SECRET_ARN"],True)
    simulate(iam,role,["secretsmanager:GetSecretValue"],variables["SPIKE_SECRET_ARN"]+"-forbidden",False)
    simulate(iam,role,["dynamodb:GetItem"],f"arn:aws:dynamodb:ap-southeast-1:{state['account']}:table/{state['outputs']['TableName']}",False)
    simulate(iam,role,["iam:PassRole","lambda:UpdateFunctionConfiguration"],"*",False)
    return function


def artifact_data(path):
    path=path.resolve();require(path.is_relative_to(ROOT/"local"))
    with ZipFile(path) as archive:
        names=archive.namelist();require(len(names)==len(set(names)))
        require(not any(Path(n).name.startswith(".env") or n.endswith(".pyd") for n in names))
        for source in (ROOT/"backend/src/enterprise_ai_platform").rglob("*"):
            if source.is_file() and source.suffix in {".py",".sql"} and "__pycache__" not in source.parts:
                require(archive.read(source.relative_to(ROOT/"backend/src").as_posix())==source.read_bytes())
    data=path.read_bytes()
    return data,base64.b64encode(hashlib.sha256(data).digest()).decode()


def deploy(session,state,artifact,*,apply=False):
    function,variables,origin,_=configuration(session,state,False)
    require(function["Handler"]=="enterprise_ai_platform.oidc_spike_lambda.handler")
    require(set(variables)<={"SPIKE_ENABLED","SPIKE_TABLE_NAME","SPIKE_SECRET_ARN","OIDC_ISSUER","OIDC_CLIENT_ID",
        "OIDC_REDIRECT_URI","PUBLIC_ORIGIN","OIDC_AUTHORIZATION_ENDPOINT","OIDC_TOKEN_ENDPOINT","OIDC_JWKS_URI",
        "APP_AUTH_ENABLED","APP_AUTH_CALLBACK_LOGGING_CONTROLLED","APP_AUTH_DEMO_UI"})
    cloud=session.client("cloudformation");template=TEMPLATE.read_text(encoding="utf-8")
    resources={x["LogicalResourceId"] for page in cloud.get_paginator("list_stack_resources").paginate(StackName=state["stack_id"])
               for x in page["StackResourceSummaries"] if x["ResourceStatus"]!="DELETE_COMPLETE"}
    require(resources==set(re.findall(r"^  (\w+):\n    Type: AWS::",template,re.M)))
    cloud.validate_template(TemplateBody=template)
    data,code_hash=artifact_data(artifact)
    stack=cloud.describe_stacks(StackName=state["stack_id"])["Stacks"][0]
    old_template=cloud.get_template(StackName=state["stack_id"])["TemplateBody"]
    old_version=session.client("secretsmanager").describe_secret(SecretId=variables["SPIKE_SECRET_ARN"])["VersionIdsToStages"]
    current_versions=[key for key,stages in old_version.items() if "AWSCURRENT" in stages]
    require(len(current_versions)==1)
    key="auth02/"+hashlib.sha256(data).hexdigest()+".zip"
    record={"phase":"prepared","account":state["account"],"stack_id":state["stack_id"],
        "function_arn":function["FunctionArn"],"old_template":old_template,"old_parameters":stack["Parameters"],
        "old_handler":function["Handler"],"old_environment":variables,"old_timeout":function["Timeout"],
        "old_role_arn":function["Role"],
        "old_code_sha256":function["CodeSha256"],"old_secret_version":current_versions[0],
        "new_artifact_key":key,"new_artifact":str(artifact.resolve()),"new_code_sha256":code_hash,
        "new_resources":0,"iam_change":"remove_dynamodb_access","new_secret_version":None}
    RECORD.parent.mkdir(parents=True,exist_ok=True)
    if RECORD.exists():
        existing=json.loads(RECORD.read_text(encoding="utf-8"))
        require(existing["phase"]=="prepared" and existing["stack_id"]==state["stack_id"])
    RECORD.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    if not apply:
        print(json.dumps({"phase":"prepared","passed":True,"new_resources":0,"auth_enabled":False,"iam_change":"remove_dynamodb_access"}))
        return
    settings=load_settings(ROOT/".env",require_file=True,settings_type=DatabaseProbeSettings)
    base=parse_target(settings.database_url.get_secret_value())
    direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
    verify_same_database(base,direct)
    async def migrate_runtime():
        revision=await migrate(direct,upgrade=True)
        require(revision==(4,4))
        database=RuntimeDatabase(base)
        try:await database.open()
        finally:await database.close()
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:runner.run(migrate_runtime())
    record["phase"]="database_migrated"
    RECORD.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    secrets_client=session.client("secretsmanager")
    private=json.loads(secrets_client.get_secret_value(SecretId=variables["SPIKE_SECRET_ARN"],VersionId=current_versions[0])["SecretString"])
    require(set(private)<={"client_secret","operator_secret","encryption_key","database_url","auth_verifier_key"})
    private["database_url"]=settings.database_url.get_secret_value()
    private["auth_verifier_key"]=Fernet.generate_key().decode()
    version=secrets_client.put_secret_value(SecretId=variables["SPIKE_SECRET_ARN"],SecretString=json.dumps(private))["VersionId"]
    record["new_secret_version"]=version;record["phase"]="secret_staged"
    RECORD.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    session.client("s3").put_object(Bucket=state["bucket"],Key=key,Body=data,ServerSideEncryption="AES256")
    old_keys={p["ParameterKey"] for p in stack["Parameters"]}
    overrides={"ArtifactKey":key,"ApplicationMode":"identity","PublicOrigin":origin,"RedirectUri":origin+"/api/v1/auth/callback"}
    parameters=[{"ParameterKey":k,"UsePreviousValue":True} for k in sorted(old_keys-set(overrides))]
    parameters.extend({"ParameterKey":k,"ParameterValue":v} for k,v in overrides.items())
    response=cloud.update_stack(StackName=state["stack_id"],TemplateBody=template,Parameters=parameters,Capabilities=["CAPABILITY_IAM"])
    require(response["StackId"]==state["stack_id"])
    record["phase"]="stack_updating"
    RECORD.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"phase":"stack_updating","new_resources":0,"auth_enabled":False,"database_revision":4}))


def rollback(session,state,record,*,finish=False):
    require(session.client("sts").get_caller_identity()["Account"]==state["account"])
    cloud=session.client("cloudformation")
    stack=cloud.describe_stacks(StackName=state["stack_id"])["Stacks"][0]
    require(stack["StackId"]==record["stack_id"]==state["stack_id"])
    client=session.client("lambda")
    function=client.get_function_configuration(FunctionName=state["function_arn"])
    require(function["FunctionArn"]==record["function_arn"] and function["Role"]==record["old_role_arn"])
    if not finish:
        # Stop auth first, without requiring the currently active IAM policy to pass.
        variables={**function["Environment"]["Variables"],"APP_AUTH_ENABLED":"false","SPIKE_ENABLED":"false"}
        client.update_function_configuration(FunctionName=state["function_arn"],RevisionId=function["RevisionId"],Environment={"Variables":variables})
        client.get_waiter("function_updated_v2").wait(FunctionName=state["function_arn"],WaiterConfig={"Delay":2,"MaxAttempts":25})
        if (stack["StackStatus"]=="UPDATE_ROLLBACK_COMPLETE" and function["Handler"]==record["old_handler"]
                and function["CodeSha256"]==record["old_code_sha256"]):
            rollback(session,state,record,finish=True)
            return
        template=record["old_template"]
        cloud.update_stack(StackName=state["stack_id"],TemplateBody=json.dumps(template) if isinstance(template,dict) else template,
            Parameters=[{"ParameterKey":p["ParameterKey"],"ParameterValue":p["ParameterValue"]} for p in record["old_parameters"]],Capabilities=["CAPABILITY_IAM"])
        record["phase"]="rollback_updating"
    else:
        require(stack["StackStatus"] in {"UPDATE_COMPLETE","UPDATE_ROLLBACK_COMPLETE"} and function["Handler"]==record["old_handler"]
                and function["CodeSha256"]==record["old_code_sha256"])
        secrets=session.client("secretsmanager")
        stages=secrets.describe_secret(SecretId=state["outputs"]["SecretArn"])["VersionIdsToStages"]
        current=[k for k,v in stages.items() if "AWSCURRENT" in v]
        require(len(current)==1 and current[0] in {record["old_secret_version"],record["new_secret_version"]})
        if current[0]!=record["old_secret_version"]:
            secrets.update_secret_version_stage(SecretId=state["outputs"]["SecretArn"],VersionStage="AWSCURRENT",
                MoveToVersionId=record["old_secret_version"],RemoveFromVersionId=current[0])
        variables={**record["old_environment"],"SPIKE_ENABLED":"false","APP_AUTH_ENABLED":"false"}
        client.update_function_configuration(FunctionName=state["function_arn"],RevisionId=function["RevisionId"],
            Environment={"Variables":variables},Timeout=record["old_timeout"])
        record["phase"]="rollback_restoring_disabled_environment"
    RECORD.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"passed":True,"phase":record["phase"],"schema_downgrade":False}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["prepare","deploy","status","enable","disable","rollback","rollback-finish"])
    parser.add_argument("--artifact",type=Path)
    args=parser.parse_args();safe_transport_logs()
    try:
        state=json.loads(STATE_FILE.read_text(encoding="utf-8"));session=boto3.Session(region_name="ap-southeast-1")
        if args.action in {"prepare","deploy"}:
            require(args.artifact is not None)
            deploy(session,state,args.artifact,apply=args.action=="deploy")
            return
        record=json.loads(RECORD.read_text(encoding="utf-8"))
        require(record["stack_id"]==state["stack_id"] and record["account"]==state["account"])
        if args.action in {"rollback","rollback-finish"}:
            rollback(session,state,record,finish=args.action=="rollback-finish")
        elif args.action=="status":
            client=session.client("lambda");flag=client.get_function_configuration(FunctionName=state["function_arn"])["Environment"]["Variables"].get("APP_AUTH_ENABLED")=="true"
            identity_status(session,state,enabled=flag)
            record["phase"]="enabled" if flag else "deployed_disabled"
            RECORD.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
            print(json.dumps({"passed":True,"phase":record["phase"],"new_resources":0,"database_revision":4,"iam_dynamodb_access":False}))
        else:
            enabled=args.action=="enable"
            if enabled:
                function=identity_status(session,state,enabled=False)
            else:
                require(session.client("sts").get_caller_identity()["Account"]==record["account"])
                function=session.client("lambda").get_function_configuration(FunctionName=record["function_arn"])
                require(function["FunctionArn"]==state["function_arn"] and function["Role"]==record["old_role_arn"])
            if enabled:
                proof=json.loads((ROOT/"local/auth02/cloud-disabled-canary.json").read_text(encoding="utf-8"))
                require(proof.get("passed") is True and proof.get("code_sha256")==record["new_code_sha256"]
                        and proof.get("stack_id")==state["stack_id"] and 0<=time.time()-proof.get("checked_at",0)<=3600
                        and proof.get("configuration_fingerprint")==configuration_fingerprint(function))
            variables={**function["Environment"]["Variables"],"APP_AUTH_ENABLED":"true" if enabled else "false"}
            session.client("lambda").update_function_configuration(FunctionName=state["function_arn"],RevisionId=function["RevisionId"],Environment={"Variables":variables})
            record["phase"]="enabling" if enabled else "disabling"
            RECORD.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
            print(json.dumps({"phase":record["phase"],"passed":True}))
    except Exception:
        print(json.dumps({"passed":False,"reason":"deployment_state_or_operation_rejected","auth_login":"not_tested"}))
        print("Stop; reconcile the local rollback record and actual stack. Do not retry deployment blindly.")
        raise SystemExit(1) from None


if __name__=="__main__":main()
