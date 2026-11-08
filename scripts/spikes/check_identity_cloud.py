"""Scoped canaries for the new API handler; no human login or credential output."""

import argparse
import hashlib
from http.cookies import SimpleCookie
import json
import secrets
import time
from urllib.parse import parse_qs,urlsplit

import boto3
import httpx
from psycopg import sql

from deploy_identity_api import ROOT,RECORD,identity_status,configuration_fingerprint
from check_oidc_runtime import STATE_FILE,safe_transport_logs
from verify_oidc_cloud import collect_complete_logs
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import MigrationSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import direct_connection,parse_target
import asyncio

CURRENT_CHECK="initialization"


def step(name):
    global CURRENT_CHECK
    CURRENT_CHECK=name


def require(condition):
    if not condition:raise ValueError("Cloud canary rejected.")


def validate_response(response,status,kind):
    step("http_"+kind+"_expected_"+str(status))
    require(response.status_code==status)
    if kind=="health_ready":
        require(response.json()=={"status":"ready","scope":"bootstrap"})
    else:
        require(response.headers.get("cache-control")=="no-store" and response.headers.get("referrer-policy")=="no-referrer")
    invocation=response.headers.get("x-eai-invocation-id");require(bool(invocation))
    return invocation


def binding(response):
    values=response.headers.get_list("set-cookie")
    require(len(values)==1)
    cookie=SimpleCookie();cookie.load(values[0]);require(set(cookie)=={"__Host-eai-login"})
    attributes=[part.strip().split("=",1)[0].lower() for part in values[0].split(";")]
    require(len(attributes)==len(set(attributes)) and "domain" not in attributes)
    item=cookie["__Host-eai-login"]
    require(item["secure"] and item["httponly"] and item["samesite"].lower()=="lax" and item["path"]=="/"
            and not item["domain"] and item["max-age"]=="300" and len(item.value)==43)
    return item.value


def authorization(response,variables):
    require(response.status_code==303)
    location=response.headers["location"]
    require(location.split("?",1)[0]==variables["OIDC_AUTHORIZATION_ENDPOINT"])
    p=parse_qs(location.split("?",1)[1],keep_blank_values=True)
    require(set(p)=={"response_type","client_id","redirect_uri","scope","state","nonce","code_challenge","code_challenge_method"})
    require(p["response_type"]==["code"] and p["client_id"]==[variables["OIDC_CLIENT_ID"]]
        and p["redirect_uri"]==[variables["OIDC_REDIRECT_URI"]] and p["scope"]==["openid profile"] and p["code_challenge_method"]==["S256"])
    require(all(len(p[k])==1 and len(p[k][0])==43 for k in ("state","nonce","code_challenge")))
    return p


async def database_snapshot(binding_hash=None,state_hash=None,*,cleanup=False):
    settings=load_settings(ROOT/".env",require_file=True,settings_type=MigrationSettings)
    target=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
    async with direct_connection(target) as connection:
        if cleanup:
            await connection.execute("DELETE FROM platform.login_transactions WHERE binding_hash=%s AND state_hash=%s",(binding_hash,state_hash))
            return {}
        sessions=(await (await connection.execute("SELECT count(*) FROM platform.sessions")).fetchone())[0]
        ips=await (await connection.execute("SELECT key,requests FROM platform.identity_limits WHERE key LIKE 'login:ip:%%' "
            "AND started_at>clock_timestamp()-interval '60 seconds'")).fetchall()
        row=None
        if binding_hash:
            row=await (await connection.execute("SELECT verifier IS NOT NULL,consumed_at IS NOT NULL FROM platform.login_transactions "
                                               "WHERE binding_hash=%s AND state_hash=%s",(binding_hash,state_hash))).fetchone()
        return {"sessions":sessions,"ip_buckets":dict(ips),"transaction":row}


def check(phase):
    step("metadata_and_iam")
    safe_transport_logs();state=json.loads(STATE_FILE.read_text(encoding="utf-8"));record=json.loads(RECORD.read_text(encoding="utf-8"))
    session=boto3.Session(region_name="ap-southeast-1")
    enabled=phase=="enabled"
    function=identity_status(session,state,enabled=enabled)
    variables=function["Environment"]["Variables"];origin=variables["PUBLIC_ORIGIN"]
    if enabled:
        proof=json.loads((ROOT/"local/auth02/cloud-disabled-canary.json").read_text(encoding="utf-8"))
        require(proof["passed"] is True and proof["code_sha256"]==function["CodeSha256"]
            and proof["stack_id"]==state["stack_id"] and 0<=time.time()-proof["checked_at"]<=3600
            and proof["configuration_fingerprint"]==configuration_fingerprint(function))
    started=int(time.time()*1000)
    marker="auth02-canary-"+secrets.token_hex(16);forbidden=[marker];invocations=[];owned=[]
    with asyncio.Runner(loop_factory=database_loop_factory) as runner:
        def snapshot(*args,**kwargs):return runner.run(database_snapshot(*args,**kwargs))
        step("database_preflight")
        before=snapshot() if enabled else None
        def record_response(response,status,kind):
            invocation=validate_response(response,status,kind)
            invocations.append((invocation,kind,status))
            return response
        try:
            with httpx.Client(trust_env=False,follow_redirects=False,timeout=55) as http:
                if not enabled:
                    record_response(http.get(origin+"/api/v1/auth/callback",params={"code":marker,"state":marker}),404,"callback")
                    record_response(http.get(origin+"/api/v1/auth/login"),404,"login")
                else:
                    record_response(http.get(origin+"/health/ready"),200,"health_ready")
                    first=record_response(http.get(origin+"/api/v1/auth/login",headers={"X-Forwarded-For":"198.18.0.1"}),303,"login")
                    p1=authorization(first,variables);cookie1=binding(first)
                    forbidden.extend([cookie1,p1["state"][0],p1["nonce"][0]])
                    one=snapshot()
                    require(one["sessions"]==before["sessions"])
                    first_hash=hashlib.sha256(("eai:binding:v1:"+cookie1).encode()).hexdigest()
                    first_state=hashlib.sha256(("eai:state:v1:"+p1["state"][0]).encode()).hexdigest()
                    owned.append((first_hash,first_state))
                    second=record_response(http.get(origin+"/api/v1/auth/login",headers={"X-Forwarded-For":"203.0.113.99"}),303,"login")
                    p2=authorization(second,variables);cookie2=binding(second)
                    forbidden.extend([cookie2,p2["state"][0],p2["nonce"][0]])
                    second_hash=hashlib.sha256(("eai:binding:v1:"+cookie2).encode()).hexdigest()
                    second_state=hashlib.sha256(("eai:state:v1:"+p2["state"][0]).encode()).hexdigest()
                    owned.append((second_hash,second_state))
                    two=snapshot(second_hash,second_state)
                    changed=[k for k,v in two["ip_buckets"].items() if v==one["ip_buckets"].get(k,0)+1]
                    require(len(changed)==1 and changed[0] in one["ip_buckets"] and snapshot(first_hash,first_state)["transaction"] is None)
                    wrong=secrets.token_urlsafe(32);forbidden.append(wrong)
                    record_response(http.get(origin+"/api/v1/auth/callback",params={"code":marker,"state":wrong}),401,"callback")
                    require(snapshot(second_hash,second_state)["transaction"]==(True,False))
                    denial={"error":"access_denied","error_description":marker,"state":p2["state"][0]}
                    record_response(http.get(origin+"/api/v1/auth/callback",params=denial),401,"callback")
                    require(snapshot(second_hash,second_state)["transaction"]==(False,True))
                    record_response(http.get(origin+"/api/v1/auth/callback",params=denial),401,"callback")
                    record_response(http.get(origin+"/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"}),401,"me")
                    record_response(http.post(origin+"/api/v1/auth/logout",headers={"Origin":origin,"Content-Type":"application/json"},content="{}"),401,"logout")
                    record_response(http.get(origin+"/api/v1/auth/login",params={"return_to":"https://unapproved.example.test"}),400,"login")
                    options=record_response(http.options(origin+"/api/v1/me",headers={"Origin":"https://unapproved.example.test",
                        "Access-Control-Request-Headers":"X-EAI-CSRF-Bootstrap"}),405,"me")
                    require("access-control-allow-origin" not in options.headers)
                    record_response(http.get(origin+"/"),200,"home")
                    require(snapshot()["sessions"]==before["sessions"])
            step("correlated_cloudwatch_logs")
            messages=[]
            for invocation,kind,status in invocations:
                current_messages=collect_complete_logs(session.client("logs"),state["outputs"]["LogGroup"],started,invocation)
                messages.extend(current_messages)
                expected=f"identity_request kind={kind} stage={'request' if enabled else 'disabled'} request_id={invocation} status={status}"
                require(any(expected in m for m in current_messages))
            require(not any(value in line for value in forbidden for line in messages))
        finally:
            original_stage=CURRENT_CHECK
            for binding_hash,state_hash in owned:
                step("owned_transaction_cleanup")
                snapshot(binding_hash,state_hash,cleanup=True)
            step(original_stage)
    result={"passed":True,"phase":phase,"checked_at":int(time.time()),"stack_id":state["stack_id"],
        "configuration_fingerprint":configuration_fingerprint(function),
        "code_sha256":function["CodeSha256"],"invocations_checked":len(invocations),"correlated_logs_complete":True,
        "synthetic_auth_values_absent_from_logs":True,"actual_user_login":"not_tested"}
    if enabled:result.update(runtime_database_ready=True,secure_binding_cookie=True,forwarded_ip_ignored=True,
        pending_preserved_on_wrong_state=True,claim_verifier_removed=True,replay_denied=True,no_application_session_created=True,
        unauthenticated_me_logout_denied=True,open_redirect_denied=True,cross_origin_preflight_denied=True,owned_transactions_cleaned=True)
    file=ROOT/f"local/auth02/cloud-{phase}-canary.json";file.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    return {k:v for k,v in result.items() if k not in {"stack_id","code_sha256"}}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("phase",choices=["disabled","enabled"]);args=parser.parse_args()
    target=ROOT/f"local/auth02/cloud-{args.phase}-canary.json"
    try:
        target.parent.mkdir(parents=True,exist_ok=True)
        # Fail closed before any cloud call. A failed run cannot retain a pass
        # proof for the same artifact from an earlier execution.
        target.write_text(json.dumps({"passed":False,"phase":args.phase,"checked_at":int(time.time()),"status":"started"}),encoding="utf-8")
    except (Exception,KeyboardInterrupt):
        print(json.dumps({"passed":False,"phase":args.phase,"reason":"proof_invalidation_failed","cloud_called":False}))
        raise SystemExit(1) from None
    try:print(json.dumps(check(args.phase),sort_keys=True))
    except (Exception,KeyboardInterrupt):
        try:
            target.write_text(json.dumps({"passed":False,"phase":args.phase,"checked_at":int(time.time())}),encoding="utf-8")
        except (Exception,KeyboardInterrupt):
            pass
        print(json.dumps({"passed":False,"phase":args.phase,"reason":"canary_rejected","failed_check":CURRENT_CHECK,"actual_user_login":"not_tested"}))
        print("Disable auth and review sanitized diagnostics. Do not retry automatically.")
        raise SystemExit(1) from None


if __name__=="__main__":main()
