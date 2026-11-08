"""Opt-in AUTH-04 HTTP/crypto/current-actor checks on disposable PostgreSQL."""

import argparse
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time
from uuid import uuid4
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa,padding
from cryptography.hazmat.primitives import hashes
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.authorization_check import Harness as AccessHarness,CheckFailure,require
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.infrastructure.database.delegation import PostgresDelegationRepository
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier,RSADelegationSigner
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.delegation.domain import HumanReceipt,request_hash,HUMAN_ACTIONS,OBJECTS
from enterprise_ai_platform.modules.delegation.service import DelegationService
from enterprise_ai_platform.modules.delegation.ports import DelegationFailure
from enterprise_ai_platform.modules.identity.service import digest

ISSUER="enterprise-ai-platform"
AUDIENCE="reference-reimbursement"
CALLER="platform-dispatcher"


class Receipts:
    def __init__(self):self.values={}
    async def get(self,receipt_id):return self.values.get(receipt_id)


class SigningFixture:
    def __init__(self):
        self.private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        self.other=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        self.jwk={**jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key(),as_dict=True),"alg":"RS256","use":"sig","kid":"current"}

    def sign(self,claims,mode=None):
        headers={"kid":"current","typ":"JWT"};key=self.private;algorithm="RS256"
        if mode=="unknown_kid":headers["kid"]="unknown"
        if mode=="wrong_signature":key=self.other
        if mode in {"jku","crit"}:headers[mode]="https://untrusted.example.test" if mode=="jku" else ["unknown"]
        if mode=="HS256":algorithm="HS256";key="fictional-hmac-secret-which-is-not-a-public-rsa-key"
        if mode=="none":algorithm="none";key=""
        if mode in {"duplicate_claim","duplicate_header"}:
            enc=lambda value:base64.urlsafe_b64encode(value.encode()).rstrip(b"=")
            h=json.dumps({**headers,"alg":"RS256"});body=json.dumps(claims)
            if mode=="duplicate_claim":body=body[:-1]+',"iss":"'+ISSUER+'"}'
            else:h=h[:-1]+',"kid":"current"}'
            message=enc(h)+b"."+enc(body)
            signature=self.private.sign(message,padding.PKCS1v15(),hashes.SHA256())
            return (message+b"."+base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()
        return jwt.encode(claims,key,algorithm=algorithm,headers=headers)


class Harness(AccessHarness):
    def __init__(self,admin,database,schema):
        super().__init__(admin,database,schema)
        self.signing=SigningFixture();self.receipts=Receipts();self.delegation_repo=PostgresDelegationRepository(database)
        self.delegation=DelegationService(self.delegation_repo,RSADelegationVerifier(),ISSUER,receipts=self.receipts)
        self.actor,self.tenant_id,self.membership=uuid4(),uuid4(),uuid4()
        self.credential=secrets.token_urlsafe(32)
        self.credential_hash=hashlib.sha256(("eai:internal-caller:v1:"+self.credential).encode()).hexdigest()

    async def reset(self,action="trip.read",roles=None):
        await self.member(self.tenant_id,self.actor,self.membership,roles if roles is not None else (RoleGrant("employee","tenant",str(self.tenant_id)),RoleGrant("approver","process","reimbursement")))
        await self.execute("DELETE FROM {s}.delegation_replays; DELETE FROM {s}.delegation_keys; DELETE FROM {s}.internal_caller_credentials")
        await self.execute("INSERT INTO {s}.internal_caller_credentials(id,caller_ref,credential_hash,audience,delegation_caller,allowed_actions,expires_at) VALUES(%s,'reference-fixture',%s,%s,%s,%s,clock_timestamp()+interval '1 day')",
                           (uuid4(),self.credential_hash,AUDIENCE,CALLER,Jsonb(["trip.read","claim.read","operation.read","claim.submit","claim.information","claim.decide"])))
        await self.execute("INSERT INTO {s}.delegation_keys VALUES(%s,'current',%s,true,clock_timestamp()-interval '1 day',clock_timestamp()+interval '1 day')",(ISSUER,Jsonb(self.signing.jwk)))
        await self.execute("UPDATE {s}.principals SET active=true WHERE id=%s",(self.actor,))
        await self.execute("UPDATE {s}.tenants SET active=true WHERE id=%s",(self.tenant_id,))
        await self.execute("UPDATE {s}.memberships SET active=true WHERE id=%s AND tenant_id=%s",(self.membership,self.tenant_id))
        token,_=await self.session(self.tenant_id,self.actor)
        session=(await (await self.execute("SELECT id FROM {s}.sessions WHERE token_hash=%s",(digest("session",token),))).fetchone())[0]
        revision=(await (await self.execute("SELECT access_revision FROM {s}.memberships WHERE tenant_id=%s AND id=%s",(self.tenant_id,self.membership))).fetchone())[0]
        now=int(time.time());human=action in HUMAN_ACTIONS
        payload={"schema_version":1,"action":action,"tenant_id":str(self.tenant_id),"actor_id":str(self.actor),"operation_id":str(uuid4()),
            "object":{"kind":OBJECTS[action],"id":str(uuid4()),"version":1},
            "data":{"revision":2,"stage":1,"decision":"reject","reason":"Receipt tidak lengkap. "} if human else {}}
        if action in {"claim.submit","claim.information"}:
            receipt_id=str(uuid4())
            payload["data"]={"trip_id":str(uuid4()),"currency":"SGD","items":[{"id":str(uuid4()),"trip_item_id":str(uuid4()),"amount_minor":100,"receipt_ids":[receipt_id]}],
                "total_minor":100,"receipts":[{"id":receipt_id,"version":1,"digest":"a"*64}],"policy":{"id":str(uuid4()),"version":1,"digest":"b"*64},
                "approval_route":[{"stage":1,"approver_id":str(uuid4()),"scope_ref":"reimbursement"}],"destination":AUDIENCE}
            if action=="claim.information":payload["data"].update(revision=2,draft={"id":str(uuid4()),"version":1})
        claims={"iss":ISSUER,"aud":AUDIENCE,"caller":CALLER,"sub":str(self.actor),"tenant_id":str(self.tenant_id),"membership_id":str(self.membership),
            "session_id":str(session),"access_revision":revision,"action":action,"object_kind":payload["object"]["kind"],"object_id":payload["object"]["id"],
            "object_version":1,"request_hash":request_hash(payload),"operation_id":payload["operation_id"],"jti":str(uuid4()),
            "iat":now,"nbf":now,"exp":now+60,"human_receipt_id":str(uuid4()) if human else None}
        self.receipts.values={}
        if human:
            receipt=HumanReceipt(claims["human_receipt_id"],claims["sub"],claims["session_id"],claims["tenant_id"],claims["operation_id"],action,
                claims["object_kind"],claims["object_id"],1,claims["request_hash"],now)
            self.receipts.values[receipt.id]=receipt
        self.delegation.receipts=self.receipts
        return payload,claims

    async def database_change(self,mode,claims):
        if mode.startswith("caller_"):
            field="active=false" if mode=="caller_revoked" else "expires_at=clock_timestamp()-interval '1 second'"
            await self.execute("UPDATE {s}.internal_caller_credentials SET "+field)
        elif mode.startswith("key_"):
            fields={"key_revoked":"active=false","key_expired":"not_before=clock_timestamp()-interval '2 days',not_after=clock_timestamp()-interval '1 day'",
                    "key_future":"not_before=clock_timestamp()+interval '1 hour',not_after=clock_timestamp()+interval '1 day'"}
            await self.execute("UPDATE {s}.delegation_keys SET "+fields[mode])
        elif mode=="principal_inactive":await self.execute("UPDATE {s}.principals SET active=false WHERE id=%s",(self.actor,))
        elif mode=="tenant_inactive":await self.execute("UPDATE {s}.tenants SET active=false WHERE id=%s",(self.tenant_id,))
        elif mode=="membership_inactive":await self.execute("UPDATE {s}.memberships SET active=false WHERE tenant_id=%s AND id=%s",(self.tenant_id,self.membership))
        elif mode=="role_revoked":await self.execute("DELETE FROM {s}.membership_roles WHERE tenant_id=%s AND membership_id=%s",(self.tenant_id,self.membership))
        elif mode=="membership_revision":await self.execute("UPDATE {s}.memberships SET access_revision=access_revision+1 WHERE tenant_id=%s AND id=%s",(self.tenant_id,self.membership))
        elif mode=="session_revoked":await self.execute("UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE id=%s",(claims["session_id"],))
        elif mode=="session_expired":await self.execute("WITH e AS(SELECT clock_timestamp()-interval '1 second' t) UPDATE {s}.sessions SET authenticated_at=e.t-interval '8 hours',created_at=e.t-interval '8 hours',last_seen_at=e.t-interval '1 hour',expires_at=e.t FROM e WHERE id=%s",(claims["session_id"],))
        elif mode=="session_idle":await self.execute("WITH e AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET authenticated_at=e.t-interval '2 hours',created_at=e.t-interval '2 hours',last_seen_at=e.t-interval '1800 seconds',expires_at=e.t+interval '6 hours' FROM e WHERE id=%s",(claims["session_id"],))
        elif mode=="action_not_allowed":await self.execute("UPDATE {s}.internal_caller_credentials SET allowed_actions='[\"claim.read\"]'")

    async def case(self,case):
        payload,claims=await self.reset(case["action"]);layer,changes=case["layer"],case["changes"]
        mode=changes.get("mode");now=int(time.time())
        if layer=="claims":
            for key,value in changes.items():
                if key=="drop":claims.pop(value)
                else:claims[key]={"$expired":now,"$future":now+30,"$future_exp":now+60,"$long":now+61,"$past":now-1}.get(value,value) if isinstance(value,str) else value
        token=self.signing.sign(claims,mode if layer=="signature" else None)
        if layer=="payload":
            for path,value in changes.items():
                target=payload;parts=path.split(".")
                for key in parts[:-1]:target=target[key]
                target[parts[-1]]=value
        if layer=="database":await self.database_change(mode,claims)
        if layer=="receipt" and mode:
            receipt=self.receipts.values[claims["human_receipt_id"]]
            if mode=="absent_reader":self.delegation.receipts=None
            elif mode=="missing":self.receipts.values.clear()
            else:
                fields={"revoked":{"revoked":True},"actor":{"actor_id":str(uuid4())},"session":{"session_id":str(uuid4())},"operation":{"operation_id":str(uuid4())},
                        "hash":{"request_hash":"f"*64},"version":{"object_version":2},"time_future":{"human_action_time":now+100}}
                self.receipts.values[receipt.id]=replace(receipt,**fields[mode])
        headers=[("Authorization","Bearer "+self.credential),("X-EAI-Delegation",token),("Content-Type","application/json")]
        body=json.dumps({"payload":payload})
        if layer=="http":
            if mode in {"missing_caller","cookie_only"}:headers=headers[1:]
            if mode=="cookie_only":headers.append(("Cookie","__Host-eai-session="+self.credential))
            if mode=="wrong_caller":headers[0]=("Authorization","Bearer "+"z"*43)
            if mode=="duplicate_authorization":headers.append(headers[0])
            if mode=="duplicate_delegation":headers.append(headers[1])
            if mode=="extra_body":body=json.dumps({"payload":payload,"role":"approver"})
            if mode=="duplicate_json":body='{"payload":'+json.dumps(payload)+',"payload":'+json.dumps(payload)+'}'
        app=create_internal_app(Settings(env="test",host="127.0.0.1",port=8000),services=ApplicationServices(health=None,delegation=self.delegation))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://internal-fixture.example.test") as client:
            response=await client.post("/internal/v1/actors/verify",headers=headers,content=body)
        require(response.status_code==case["expected_status"],case["id"]+"-status-"+str(response.status_code))
        require(response.headers.get("cache-control")=="no-store",case["id"]+"-cache")
        require(self.credential not in response.text and token not in response.text,case["id"]+"-redaction")
        if response.status_code==200:require(response.json()["allow"] is True and response.json()["request_hash"]==claims["request_hash"],case["id"]+"-allow")

    async def regressions(self):
        checked=[];payload,claims=await self.reset()
        token=self.signing.sign(claims)
        first=await self.delegation.verify(self.credential,token,payload)
        again=await self.delegation.verify(self.credential,token,payload)
        require(not first["replayed"] and again["replayed"],"stable-retry")
        checked.append("identical_verification_retry_is_reauthorized_not_reexecuted")
        outcomes=await asyncio.gather(*(self.delegation.verify(self.credential,token,payload) for _ in range(3)))
        require(all(x["replayed"] for x in outcomes),"concurrent-retry")
        require((await (await self.execute("SELECT count(*) FROM {s}.delegation_replays")).fetchone())[0]==1,"one-replay-row")
        checked.append("concurrent_verification_single_replay_binding")
        altered=json.loads(json.dumps(payload));altered["operation_id"]=str(uuid4())
        other={**claims,"operation_id":altered["operation_id"],"request_hash":request_hash(altered)}
        try:await self.delegation.verify(self.credential,self.signing.sign(other),altered)
        except DelegationFailure as error:require(error.status==409,"changed-jti-binding")
        else:raise CheckFailure("changed-jti-binding-accepted")
        checked.append("same_jti_changed_operation_rejected")
        await self.execute("UPDATE {s}.memberships SET active=false WHERE tenant_id=%s AND id=%s",(self.tenant_id,self.membership))
        try:await self.delegation.verify(self.credential,token,payload)
        except DelegationFailure as error:require(error.status==403,"replay-current-membership")
        else:raise CheckFailure("revoked-replay-accepted")
        checked.append("replay_cannot_bypass_membership_revocation")
        payload,claims=await self.reset();token=self.signing.sign(claims)
        await self.delegation.verify(self.credential,token,payload)
        second=SigningFixture()
        await self.execute("INSERT INTO {s}.delegation_keys VALUES(%s,'next',%s,true,clock_timestamp()-interval '1 hour',clock_timestamp()+interval '1 day')",(ISSUER,Jsonb({**second.jwk,"kid":"next"})))
        signed=jwt.encode({**claims,"jti":str(uuid4())},second.private,algorithm="RS256",headers={"kid":"next"})
        await self.delegation.verify(self.credential,signed,payload)
        await self.execute("UPDATE {s}.delegation_keys SET active=false WHERE kid='current'")
        try:await self.delegation.verify(self.credential,token,payload)
        except DelegationFailure as error:require(error.status==401,"retired-key")
        else:raise CheckFailure("retired-key-accepted")
        checked.append("overlap_key_rotation_then_retired_key_denied")
        # A key or credential changed after preflight is checked again in the final DB transaction.
        original=self.delegation_repo.call
        for mutation,expected in (("key",403),("credential",401)):
            payload,claims=await self.reset();token=self.signing.sign(claims)
            async def intercepted(operation,arguments):
                if operation=="verify":
                    if mutation=="key":await self.execute("UPDATE {s}.delegation_keys SET public_jwk=%s WHERE kid='current'",(Jsonb(SigningFixture().jwk),))
                    else:await self.execute("UPDATE {s}.internal_caller_credentials SET active=false")
                return await original(operation,arguments)
            self.delegation_repo.call=intercepted
            try:await self.delegation.verify(self.credential,token,payload)
            except DelegationFailure as error:require(error.status==expected,"inflight-rotation-"+mutation)
            else:raise CheckFailure("inflight-rotation-accepted")
            finally:self.delegation_repo.call=original
            checked.append("inflight_"+mutation+"_revocation_rechecked")
        payload,claims=await self.reset("claim.decide");receipt=self.receipts.values[claims["human_receipt_id"]]
        reader=self.receipts.get;reads=0
        async def changing_reader(receipt_id):
            nonlocal reads
            reads+=1
            return receipt if reads==1 else replace(receipt,revoked=True)
        self.receipts.get=changing_reader
        try:await self.delegation.verify(self.credential,self.signing.sign(claims),payload)
        except DelegationFailure as error:require(error.status==403,"receipt-recheck")
        else:raise CheckFailure("changed-human-receipt-accepted")
        finally:self.receipts.get=reader
        checked.append("human_receipt_rechecked_before_allow")
        for event,expected in (("expiry",401),("logout",403)):
            payload,claims=await self.reset("claim.decide")
            if event=="expiry":claims["exp"]=int(time.time())+2
            reader=self.receipts.get;reads=0
            async def delayed_receipt(receipt_id):
                nonlocal reads
                reads+=1
                if reads==2:
                    if event=="expiry":await asyncio.sleep(2.1)
                    else:await self.execute("UPDATE {s}.sessions SET revoked_at=clock_timestamp() WHERE id=%s",(claims["session_id"],))
                return await reader(receipt_id)
            self.receipts.get=delayed_receipt
            try:await self.delegation.verify(self.credential,self.signing.sign(claims),payload)
            except DelegationFailure as error:require(error.status==expected,"last-receipt-"+event)
            else:raise CheckFailure("late-receipt-allow")
            finally:self.receipts.get=reader
            checked.append(event+"_during_last_receipt_read_denied")
        payload,claims=await self.reset(roles=(RoleGrant("tenant_admin","tenant",str(self.tenant_id)),))
        try:await self.delegation.verify(self.credential,self.signing.sign(claims),payload)
        except DelegationFailure as error:require(error.status==403,"admin-no-business-role")
        else:raise CheckFailure("admin-given-business-access")
        checked.append("admin_has_no_implicit_business_authority")
        payload,claims=await self.reset("claim.read",roles=(RoleGrant("approver","process","reimbursement"),))
        require((await self.delegation.verify(self.credential,self.signing.sign(claims),payload))["scope"]=="actor_only","approver-actor-only")
        checked.append("approver_actor_check_preserves_downstream_object_boundary")
        stamp=(await (await self.execute("SELECT last_seen_at=authenticated_at FROM {s}.sessions WHERE id=%s",(claims["session_id"],))).fetchone())[0]
        require(stamp,"verification-should-not-refresh-idle")
        checked.append("internal_verification_does_not_extend_browser_session")
        payload,claims=await self.reset()
        await self.execute("INSERT INTO {s}.delegation_replays(issuer,jti,binding_hash,operation_id,expires_at) SELECT %s,gen_random_uuid(),repeat('a',64),gen_random_uuid(),clock_timestamp()+interval '5 minutes' FROM generate_series(1,100000)",(ISSUER,))
        try:await self.delegation.verify(self.credential,self.signing.sign(claims),payload)
        except DelegationFailure as error:require(error.status==503,"replay-capacity")
        else:raise CheckFailure("unbounded-replay-store")
        checked.append("replay_store_capacity_fails_closed")
        for table in ("internal_caller_credentials","delegation_keys","delegation_replays"):
            try:
                async with self.database.transaction() as connection:
                    await connection.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(self.schema),sql.Identifier(table)))
            except Exception:pass
            else:raise CheckFailure("private-internal-table-readable")
        checked.append("internal_tables_private_runtime")
        return checked


async def verify(direct,base,fixture,regressions_only=False):
    schema="db03_probe_"+uuid4().hex;database=None;owned=False;stage="namespace"
    try:
        async with direct_connection(direct) as connection:
            require((await (await connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s",(schema,))).fetchone()) is None,"namespace")
        owned=True;stage="migration";await migrate(direct,upgrade=True,schema=schema)
        target=await provision_role(direct,base,schema=schema,role_name="eai_runtime_"+schema.rsplit("_",1)[1])
        database=RuntimeDatabase(target,schema=schema);await database.open()
        checked=[]
        async with direct_connection(direct,timeout=600) as connection:
            harness=Harness(connection,database,schema)
            for case in ([] if regressions_only else fixture["cases"]):
                stage=case["id"];await harness.case(case);checked.append(stage)
                if len(checked)%10==0:print("Delegation cases checked: "+str(len(checked)),flush=True)
            stage="regressions";extra=await harness.regressions()
        return {"passed":True,"scope":"regressions" if regressions_only else "contract_and_regressions","contract_version":fixture["contract_version"],
            "contract_cases":checked,"regressions":extra,"real_postgres_restricted_runtime":True,"human_receipt_source":"controlled_immutable_fixture",
            "business_transactions_executed":False,"disposable_cleanup":"completed"}
    except Exception as error:
        raise CheckFailure(stage+":"+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--live",action="store_true")
    parser.add_argument("--regressions-only",action="store_true");parser.add_argument("--env-file",type=Path,default=Path(".env"))
    parser.add_argument("--output",type=Path,default=Path("local/auth04/contract-live.json"));args=parser.parse_args()
    if not args.live:parser.error("Explicit --live is required.")
    safe_transport_logs()
    try:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text('{"passed":false,"status":"running"}',encoding="utf-8")
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        fixture=json.loads(Path("backend/tests/contracts/delegation_cases.json").read_text(encoding="utf-8"))
        require([c["id"] for c in fixture["cases"]]==[f"SEC-DELEGATION-{i:03}" for i in range(1,71)],"fixture-ids")
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,fixture,args.regressions_only))
        args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8");print(json.dumps(result,sort_keys=True))
    except Exception as error:
        print("Delegation verification failed: "+(str(error) if isinstance(error,CheckFailure) else "sanitized_operation_failure"),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=="__main__":main()
