"""Explicit AUTH-02 verification with fictional data in a disposable real PostgreSQL schema."""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import logging
import io
import inspect
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
import httpx
import jwt
from psycopg import sql
from psycopg.types.json import Jsonb

from enterprise_ai_platform.composition import ApplicationServices, database_loop_factory
from enterprise_ai_platform.config import Settings, DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, direct_connection, verify_same_database, parse_target
from enterprise_ai_platform.infrastructure.database.migration import migrate, access_role
from enterprise_ai_platform.infrastructure.database.roles import provision_role, drop_probe_schema
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.integrations.identity.authentication import CognitoAuthentication, FernetVerifierProtector
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.modules.identity.domain import CognitoProvider
from enterprise_ai_platform.modules.identity.service import IdentityService, digest
from enterprise_ai_platform.modules.identity.ports import IdentityFailure


ORIGIN = "https://auth-fixture.example.test"
ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test"
DOMAIN = "https://fixture.auth.ap-southeast-1.amazoncognito.com"
SESSION = "__Host-eai-session"
LOGIN = "__Host-eai-login"
NOW = 1800000000


class VerificationFailure(RuntimeError):
    pass


def require(value):
    if not value:
        raise VerificationFailure("Identity verification assertion failed at check line "+str(inspect.currentframe().f_back.f_lineno)+".")


class ProviderFixture:
    def __init__(self):
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key = {**jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key(), as_dict=True),
                    "kid": "fixture-key", "alg": "RS256", "use": "sig"}
        self.provider = CognitoProvider(ISSUER, "synthetic-client", "synthetic-secret", ORIGIN + "/api/v1/auth/callback",
            ORIGIN, DOMAIN + "/oauth2/authorize", DOMAIN + "/oauth2/token", ISSUER + "/.well-known/jwks.json")
        self.reset()

    def reset(self):
        self.changes = {}
        self.jwks_mode = "ok"
        self.token_mode = "ok"
        self.tokens = self.fetches = 0
        self.nonce = None
        self.challenge = None
        self.subject = "fictional-person-"+uuid4().hex

    def token(self):
        changes = dict(self.changes)
        alg = changes.pop("alg", "RS256")
        signature = changes.pop("signature", None)
        duplicate = changes.pop("duplicate_json_claim", None)
        header = {"kid": changes.pop("kid", self.key["kid"])}
        if "jku" in changes:
            header["jku"] = changes.pop("jku")
        claims = {"iss": ISSUER, "sub": self.subject, "aud": "synthetic-client", "iat": NOW-10,
                  "exp": NOW+300, "nonce": self.nonce, "token_use": "id"}
        for k, v in changes.items():
            if v is None:
                claims.pop(k, None)
            else:
                claims[k] = v
        if duplicate:
            encoded_header = base64.urlsafe_b64encode(json.dumps({**header,"alg":"RS256"}).encode()).rstrip(b"=")
            body = json.dumps(claims)[:-1]+',"aud":"synthetic-client"}'
            encoded_body = base64.urlsafe_b64encode(body.encode()).rstrip(b"=")
            signing = encoded_header+b"."+encoded_body
            sig = self.private.sign(signing, padding.PKCS1v15(), hashes.SHA256())
            return (signing+b"."+base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()
        key = self.other if signature == "wrong_key" else self.private
        if alg == "none":
            key = ""
        elif alg == "HS256":
            key = "synthetic-hmac-key-not-an-rsa-key-123456789"
        return jwt.encode(claims, key, algorithm=alg, headers=header)

    async def handle(self, request):
        if str(request.url) == self.provider.token_endpoint:
            self.tokens += 1
            if self.token_mode == "timeout":
                raise httpx.ReadTimeout("synthetic-private-provider-input")
            form = parse_qs(request.content.decode())
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            if self.token_mode == "wrong_verifier" or challenge != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.token()})
        require(str(request.url) == self.provider.jwks_uri)
        self.fetches += 1
        if self.jwks_mode == "unavailable":
            raise httpx.ConnectError("synthetic-private-provider-input")
        if self.jwks_mode == "large":
            return httpx.Response(200, content=b' ' * 262145)
        keys = [self.key]
        if self.jwks_mode == "duplicate":
            keys *= 2
        if self.jwks_mode == "removed":
            keys = [{**self.key, "kid": "replacement-key"}]
        return httpx.Response(200, json={"keys": keys})


class Harness:
    def __init__(self, direct, database, schema, target):
        self.direct, self.database, self.schema, self.target = direct, database, schema, target
        self.fixture = ProviderFixture()
        self.repo = PostgresIdentityRepository(database)
        self.protector = FernetVerifierProtector(Fernet.generate_key().decode())
        self.oidc = CognitoAuthentication(self.fixture.provider, self.repo,
            transport=httpx.MockTransport(self.fixture.handle), clock=lambda: NOW)
        self.service = IdentityService(self.fixture.provider, self.repo, self.oidc, self.protector)

    async def admin(self, statement, params=None):
        async with direct_connection(self.direct, timeout=60) as connection:
            return await (await connection.execute(sql.SQL(statement).format(s=sql.Identifier(self.schema)), params)).fetchall()

    async def execute(self, statement, params=None):
        async with direct_connection(self.direct, timeout=60) as connection:
            await connection.execute(sql.SQL(statement).format(s=sql.Identifier(self.schema)), params)

    async def reset(self):
        await self.execute("DELETE FROM {s}.sessions; DELETE FROM {s}.login_transactions; DELETE FROM {s}.identity_limits; "
            "DELETE FROM {s}.identity_negative_keys; DELETE FROM {s}.identity_jwks;")
        self.fixture.reset()

    @asynccontextmanager
    async def client(self, *, service=None, cookies=None, ip="198.51.100.1"):
        services = ApplicationServices(health=None, identity=service or self.service)
        app = create_app(Settings(env="test", host="127.0.0.1", port=8000), services=services)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip,12345)),
                base_url=ORIGIN, follow_redirects=False, cookies=cookies) as client:
            yield client

    async def start(self, client, return_to="home"):
        result = await client.get("/api/v1/auth/login", params={"return_to": return_to})
        require(result.status_code == 303)
        params = parse_qs(result.headers["location"].split("?",1)[1])
        self.fixture.nonce, self.fixture.challenge = params["nonce"][0], params["code_challenge"][0]
        cookie = result.headers["set-cookie"].lower()
        require("secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "domain=" not in cookie)
        return params

    async def callback(self, client, params, **extra):
        return await client.get("/api/v1/auth/callback", params={"code":"synthetic-code","state":params["state"][0],**extra})

    async def signed_in(self, client):
        params = await self.start(client)
        require((await self.callback(client, params)).status_code == 303)
        result = await client.get("/api/v1/me", headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})
        require(result.status_code == 200)
        return result.json()

    def headers(self, csrf):
        return {"Origin":ORIGIN,"Content-Type":"application/json","X-CSRF-Token":csrf}

    async def memberships(self, principal):
        tenant, membership = uuid4(), uuid4()
        async with direct_connection(self.direct) as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("INSERT INTO {s}.tenants(id,slug,name,timezone) VALUES(%s,%s,'Fictional tenant','UTC')").format(s=sql.Identifier(self.schema)),(tenant,"fixture-"+tenant.hex))
                await connection.execute(sql.SQL("INSERT INTO {s}.tenant_configs(tenant_id,id,config_version,assistant_name,locales,help_contact,"
                    "instruction_text,connector_ref,model_policy_ref,schema_version) VALUES(%s,%s,1,'Fixture','[\"en\"]','Help','Approved sources','fixture','fixture',1)").format(s=sql.Identifier(self.schema)),(tenant,uuid4()))
                await connection.execute(sql.SQL("INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES(%s,%s,%s)").format(s=sql.Identifier(self.schema)),(tenant,membership,principal))
        return str(tenant), str(membership)

    async def expire(self, *, absolute=False, idle=False):
        hours = 8 if absolute else 1
        idle_seconds = 1800 if idle else 10
        await self.execute("WITH stamp AS (SELECT clock_timestamp() t) UPDATE {s}.sessions SET "
            "authenticated_at=stamp.t-make_interval(hours=>%s),created_at=stamp.t-make_interval(hours=>%s),"
            "expires_at=stamp.t-make_interval(hours=>%s)+interval '8 hours',"
            "last_seen_at=stamp.t-make_interval(secs=>%s) FROM stamp",(hours,hours,hours,idle_seconds))

    async def case(self, case):
        layer, changes = case["layer"], case["changes"]
        await self.reset()
        async with self.client() as client:
            if layer == "oidc":
                params = await self.start(client)
                self.fixture.changes = changes
                result = await self.callback(client, params)
                accepted = case["expected"] == "accept_valid_signed_token"
                require((result.status_code == 303) == accepted)
                count = (await self.admin("SELECT count(*) FROM {s}.sessions"))[0][0]
                require(count == int(accepted))
            elif layer == "settings":
                provider = self.fixture.provider
                try:
                    replace(provider, **changes)
                except ValueError:
                    require(self.fixture.tokens == self.fixture.fetches == 0)
                else:
                    raise VerificationFailure()
            elif layer == "redirect":
                if changes["return_to"] == "tenant_picker":
                    params = await self.start(client, "tenant_picker")
                    result = await self.callback(client, params)
                    require(result.status_code == 303 and result.headers["location"] == "/tenants")
                else:
                    result = await client.get("/api/v1/auth/login", params=changes)
                    require(result.status_code == 400 and self.fixture.tokens == 0)
            elif layer == "csrf_bootstrap":
                identity = await self.signed_in(client)
                before = await self.admin("SELECT csrf_hash,last_seen_at FROM {s}.sessions")
                baseline = {"fetch_site":"same-origin","bootstrap_header":"1","origin":ORIGIN}
                baseline.update(changes)
                names={"fetch_site":"Sec-Fetch-Site","bootstrap_header":"X-EAI-CSRF-Bootstrap","origin":"Origin"}
                headers={names[k]:v for k,v in baseline.items() if v is not None}
                result=await client.get("/api/v1/me",headers=headers)
                after=await self.admin("SELECT csrf_hash,last_seen_at FROM {s}.sessions")
                accept=case["expected"]=="rotate_no_idle_extension"
                require(result.status_code==(200 if accept else 403))
                require(after[0][1]==before[0][1] and ((after[0][0]!=before[0][0])==accept))
            elif layer == "mutation":
                identity=await self.signed_in(client)
                headers=self.headers(identity["csrf_token"])
                if changes.get("csrf_matches") is False:headers["X-CSRF-Token"]="B"*43
                for key,header in [("origin","Origin"),("content_type","Content-Type")]:
                    if key in changes:
                        if changes[key] is None:headers.pop(header,None)
                        else:headers[header]=changes[key]
                result=await client.post("/api/v1/auth/logout",headers=headers,content="{}")
                require(result.status_code==403)
                require((await self.admin("SELECT count(*) FROM {s}.sessions WHERE revoked_at IS NOT NULL"))[0][0]==0)
            else:
                await self.more_case(case, client)

    async def more_case(self, case, client):
        layer, c=case["layer"],case["changes"]
        if layer=="callback":
            params=await self.start(client)
            query={"code":"synthetic-code","state":params["state"][0]}
            if c.get("state_matches") is False:query["state"]="B"*43
            if c.get("browser_binding_matches") is False:
                client.cookies.clear()
                client.cookies.set(LOGIN,"B"*43,domain="auth-fixture.example.test",path="/")
            if c.get("transaction_age_seconds")==300:
                await self.execute("WITH stamp AS(SELECT clock_timestamp() t) UPDATE {s}.login_transactions "
                    "SET created_at=stamp.t-interval '300 seconds',expires_at=stamp.t FROM stamp")
            if c.get("consumed"):
                await self.execute("UPDATE {s}.login_transactions SET consumed_at=clock_timestamp(),verifier=NULL")
            if c.get("code_and_error"):query["error"]="access_denied"
            if "iss" in c:query["iss"]=c["iss"]
            if c.get("code_bytes"):query["code"]="x"*c["code_bytes"]
            if c.get("token_exchange"):self.fixture.token_mode="timeout"
            if c.get("pkce_verifier"):self.fixture.token_mode="wrong_verifier"
            if c.get("parallel_callbacks"):
                responses=await asyncio.gather(client.get("/api/v1/auth/callback",params=query),
                    client.get("/api/v1/auth/callback",params=query))
                require(sorted(x.status_code for x in responses)==[303,401])
                require(self.fixture.tokens==1)
                require((await self.admin("SELECT count(*) FROM {s}.sessions"))[0][0]==1)
                return
            if c.get("event")=="restart_between_login_callback":
                second=RuntimeDatabase(self.target,schema=self.schema)
                try:
                    await second.open()
                    repo=PostgresIdentityRepository(second)
                    oidc=CognitoAuthentication(self.fixture.provider,repo,transport=httpx.MockTransport(self.fixture.handle),clock=lambda:NOW)
                    service=IdentityService(self.fixture.provider,repo,oidc,self.protector)
                    async with self.client(service=service,cookies=client.cookies) as restarted:
                        require((await restarted.get("/api/v1/auth/callback",params=query)).status_code==303)
                finally:await second.close()
                require(self.fixture.tokens==1)
                return
            if c.get("event")=="same_email_different_issuer_subject":
                self.fixture.changes={"email":"same-fictional@example.test"}
                require((await client.get("/api/v1/auth/callback",params=query)).status_code==303)
                first=(await self.admin("SELECT principal_id FROM {s}.sessions WHERE revoked_at IS NULL"))[0][0]
                await self.memberships(first)
                self.fixture.changes={"email":"same-fictional@example.test","sub":"other-fictional-person"}
                params=await self.start(client)
                require((await self.callback(client,params)).status_code==303)
                active=(await self.admin("SELECT principal_id FROM {s}.sessions WHERE revoked_at IS NULL"))[0][0]
                require(active!=first)
                require((await self.admin("SELECT count(*) FROM {s}.memberships WHERE principal_id=%s",(active,)))[0][0]==0)
                return
            commit_fail=c.get("database_commit") is not None
            if commit_fail:
                await self.execute("CREATE FUNCTION {s}._fixture_fail_commit() RETURNS trigger LANGUAGE plpgsql "
                    "SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'Synthetic commit rejection.'; END $$; "
                    "REVOKE ALL ON FUNCTION {s}._fixture_fail_commit() FROM PUBLIC; "
                    "CREATE CONSTRAINT TRIGGER fixture_fail_commit AFTER INSERT ON {s}.sessions "
                    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {s}._fixture_fail_commit()")
            try:
                raw=urlencode(query)
                if c.get("duplicate_parameter"):raw+="&state="+query["state"]
                response=await client.get("/api/v1/auth/callback?"+raw)
                require(response.status_code in {400,401,503})
                require(SESSION not in response.headers.get("set-cookie",""))
                require((await self.admin("SELECT count(*) FROM {s}.sessions"))[0][0]==0)
                if c.get("state_matches") is False:
                    require((await self.admin("SELECT verifier IS NOT NULL,consumed_at IS NULL FROM {s}.login_transactions"))[0]==(True,True))
                if c.get("token_exchange") or c.get("pkce_verifier"):
                    require(self.fixture.tokens==1)
                    require((await self.admin("SELECT verifier IS NULL,consumed_at IS NOT NULL FROM {s}.login_transactions"))[0]==(True,True))
                    replay=await client.get("/api/v1/auth/callback?"+raw)
                    require(replay.status_code==401 and self.fixture.tokens==1)
                elif not commit_fail:require(self.fixture.tokens==0)
            finally:
                if commit_fail:await self.execute("DROP TRIGGER fixture_fail_commit ON {s}.sessions; DROP FUNCTION {s}._fixture_fail_commit()")
        elif layer in {"session","rotation"}:
            identity=await self.signed_in(client)
            csrf=identity["csrf_token"]
            old_token=client.cookies.get(SESSION)
            if c.get("database")=="unavailable":
                class Unavailable:
                    async def call(self,*args):raise RuntimeError("synthetic-private-database-input")
                original=self.service.repository;self.service.repository=Unavailable()
                try:
                    response=await (client.post("/api/v1/auth/logout",headers=self.headers(csrf),json={})
                        if layer=="rotation" else client.get("/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"}))
                    require(response.status_code==503 and "set-cookie" not in response.headers)
                finally:self.service.repository=original
                return
            if c.get("age_seconds"):await self.expire(absolute=True)
            if c.get("idle_seconds"):await self.expire(idle=True)
            if c.get("revoked"):await self.execute("UPDATE {s}.sessions SET revoked_at=clock_timestamp()")
            if c.get("principal_active") is False:
                await self.execute("UPDATE {s}.principals SET active=false WHERE id=%s",(identity["principal_id"],))
            if c.get("event")=="repeated_get_me_polling":
                before=await self.admin("SELECT last_seen_at FROM {s}.sessions")
                for _ in range(3):require((await client.get("/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})).status_code==200)
                require(await self.admin("SELECT last_seen_at FROM {s}.sessions")==before)
                await self.expire(idle=True)
            if any(k in c for k in ("age_seconds","idle_seconds","revoked","principal_active")) or c.get("event")=="repeated_get_me_polling":
                result=await client.get("/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})
                require(result.status_code==401)
                require("max-age=0" in result.headers["set-cookie"].lower())
                return
            if c.get("event")=="logout_then_replay_old_cookie":
                require((await client.post("/api/v1/auth/logout",headers=self.headers(csrf),json={})).status_code==204)
                response=await client.get("/api/v1/me",headers={"Cookie":SESSION+"="+old_token,"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})
                require(response.status_code==401)
                return
            if c.get("event")=="get_me_twice_then_use_first_csrf":
                require((await client.get("/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})).status_code==200)
                require((await client.post("/api/v1/auth/logout",headers=self.headers(csrf),json={})).status_code==403)
                require((await self.admin("SELECT revoked_at FROM {s}.sessions"))[0][0] is None)
                return
            owner=identity["principal_id"]
            if c.get("target_membership")=="other_principal":
                other=uuid4()
                await self.execute("INSERT INTO {s}.principals(id,issuer,subject) VALUES(%s,%s,%s)",(other,ISSUER,str(other)))
                owner=str(other)
            tenant,membership=await self.memberships(owner)
            before=(await self.admin("SELECT authenticated_at,expires_at FROM {s}.sessions WHERE revoked_at IS NULL"))[0]
            response=await client.post("/api/v1/session/tenant",headers=self.headers(csrf),json={"membership_id":membership})
            if owner!=identity["principal_id"]:
                require(response.status_code==403)
                require((await self.admin("SELECT count(*) FROM {s}.sessions WHERE revoked_at IS NULL"))[0][0]==1)
                return
            require(response.status_code==200)
            require(client.cookies.get(SESSION)!=old_token and response.json()["csrf_token"]!=csrf)
            require(response.json()["active_tenant_id"]==tenant)
            require(not any(k in response.json() for k in ("run_id","conversation_id","draft_id","approval_id")))
            require((await self.admin("SELECT authenticated_at,expires_at FROM {s}.sessions WHERE revoked_at IS NULL"))[0]==before)
            async with self.client() as replay_client:
                require((await replay_client.get("/api/v1/me",headers={"Cookie":SESSION+"="+old_token,"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})).status_code==401)
            if c.get("membership_active") is False:
                await self.execute("UPDATE {s}.memberships SET active=false WHERE id=%s",(membership,))
                try:await self.service.resolve(client.cookies.get(SESSION))
                except IdentityFailure as error:require(error.status==403)
                else:raise VerificationFailure()
                result=await client.get("/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"})
                require(result.status_code==200 and result.json()["active_tenant_id"] is None)
                require((await client.post("/api/v1/auth/logout",headers=self.headers(result.json()["csrf_token"]),json={})).status_code==204)
        elif layer=="jwks":
            params=await self.start(client)
            if c.get("kid"):self.fixture.changes={"kid":c["kid"]}
            if c.get("refresh"):self.fixture.jwks_mode=c["refresh"]
            if c.get("kid_matches"):self.fixture.jwks_mode="duplicate"
            if c.get("jku"):self.fixture.changes={"jku":c["jku"]}
            if c.get("known_key_cache_age_seconds"):
                await self.execute("INSERT INTO {s}.identity_jwks(issuer,keys,fetched_at) VALUES(%s,%s,clock_timestamp()-interval '3600 seconds')",
                                   (ISSUER,Jsonb([self.fixture.key])))
            if c.get("key_removed_on_successful_refresh"):
                await self.oidc.keys(self.fixture.key["kid"])
                await self.execute("UPDATE {s}.identity_jwks SET fetched_at=clock_timestamp()-interval '15 minutes',attempted_at=clock_timestamp()-interval '61 seconds'")
                self.fixture.jwks_mode="removed"
            if c.get("response_bytes"):
                await self.oidc.keys(self.fixture.key["kid"])
                await self.execute("UPDATE {s}.identity_jwks SET fetched_at=clock_timestamp()-interval '15 minutes',attempted_at=clock_timestamp()-interval '61 seconds'")
                self.fixture.jwks_mode="large"
            if c.get("unknown_kid_requests_in_one_minute"):
                await self.oidc.keys(self.fixture.key["kid"])
                async with self.database.transaction() as connection:
                    cursor=await connection.execute(sql.SQL("SELECT count(*) FILTER(WHERE (result->>'refresh')::boolean) FROM "
                        "(SELECT {s}.identity_call('jwks_reserve',jsonb_build_object('issuer',%s::text,'kid','unknown-'||n,'lease_hash',repeat('a',64))) result "
                        "FROM generate_series(1,1000) n) calls").format(s=sql.Identifier(self.schema)),(ISSUER,))
                    rows=await cursor.fetchall()
                require(rows[0][0]==0)
                require(self.fixture.fetches==1)
                return
            response=await self.callback(client,params)
            if c.get("response_bytes"):
                require(response.status_code==303)
                keys=(await self.admin("SELECT keys FROM {s}.identity_jwks WHERE issuer=%s",(ISSUER,)))[0][0]
                require(keys==[self.fixture.key])
            else:
                require(response.status_code in {401,503})
                require((await self.admin("SELECT count(*) FROM {s}.sessions"))[0][0]==0)
            if c.get("jku"):require(self.fixture.fetches==0)
        elif layer=="limiter":
            binding="A"*43
            if c.get("store")=="unavailable":
                class Unavailable:
                    async def call(self,*args):raise RuntimeError("synthetic-private-input")
                original=self.service.repository;self.service.repository=Unavailable()
                try:require((await client.get("/api/v1/auth/login")).status_code==503)
                finally:self.service.repository=original
            elif c.get("active_buckets"):
                await self.execute("INSERT INTO {s}.identity_limits(key,started_at,requests) SELECT 'login:ip:'||"
                    "encode(sha256(convert_to(n::text,'UTF8')),'hex'),clock_timestamp(),1 FROM generate_series(1,100000) n")
                require((await client.get("/api/v1/auth/login")).status_code==503)
            elif c.get("unknown_state_requests"):
                for _ in range(10):
                    require((await client.get("/api/v1/auth/callback",params={"code":"synthetic-code","state":"A"*43})).status_code==401)
                require(self.fixture.tokens==self.fixture.fetches==0)
            else:
                for i in range(11):
                    if c.get("same_binding_new_ips_requests"):
                        async with self.client(ip=f"198.51.100.{i+1}") as other:
                            response=await other.get("/api/v1/auth/login",headers={"Cookie":LOGIN+"="+binding})
                    else:
                        response=await client.get("/api/v1/auth/login",headers={"Cookie":LOGIN+"="+str(i)*43})
                    if c.get("elapsed_seconds")==60 and i==10:
                        await self.execute("UPDATE {s}.identity_limits SET started_at=clock_timestamp()-interval '60 seconds'")
                        response=await client.get("/api/v1/auth/login")
                        require(response.status_code==303)
                    else:require(response.status_code==(303 if i<10 else 429))
        elif layer=="logging":
            stream=io.StringIO();handler=logging.StreamHandler(stream)
            loggers=[logging.getLogger(n) for n in ("enterprise_ai_platform.http","httpx","httpcore","psycopg.pool")]
            levels=[l.level for l in loggers]
            for logger in loggers:logger.addHandler(handler);logger.setLevel(logging.DEBUG)
            try:
                params=await self.start(client)
                marker="synthetic-private-provider-input"
                response=await client.get("/api/v1/auth/callback",params={"state":params["state"][0],"error":"access_denied","error_description":marker})
                require(response.status_code==401)
                text=stream.getvalue()
                require(marker not in text and params["state"][0] not in text and params["nonce"][0] not in text)
                require(client.cookies.get(LOGIN) not in text)
            finally:
                for logger,level in zip(loggers,levels):logger.removeHandler(handler);logger.setLevel(level)
        else:raise VerificationFailure("Unimplemented identity verification case.")

    async def regressions(self):
        completed=[]
        await self.reset()
        await self.oidc.keys(self.fixture.key["kid"])
        original=await self.admin("SELECT keys FROM {s}.identity_jwks")
        try:await self.repo.call("jwks_reserve",{"issuer":ISSUER,"kid":"known"})
        except IdentityFailure:pass
        else:raise VerificationFailure()
        for operation in ("jwks_publish","jwks_failure"):
            result=await self.repo.call(operation,{"issuer":ISSUER,"kid":"known","keys":[self.fixture.key]})
            require(result["status"]=="unavailable")
        require(await self.admin("SELECT keys FROM {s}.identity_jwks")==original)
        lease="a"*64
        await self.execute("UPDATE {s}.identity_jwks SET lease_hash=%s,lease_until=clock_timestamp()-interval '1 second'",(lease,))
        result=await self.repo.call("jwks_publish",{"issuer":ISSUER,"kid":"known","lease_hash":lease,"keys":[self.fixture.key]})
        require(result["status"]=="unavailable")
        completed.append("null_and_expired_lease_denied")

        await self.reset()
        keys=await asyncio.gather(self.oidc.keys(self.fixture.key["kid"]),self.oidc.keys(self.fixture.key["kid"]))
        require(keys[0]==keys[1]==[self.fixture.key] and self.fixture.fetches==1)
        completed.append("shared_jwks_single_flight")

        await self.execute("INSERT INTO {s}.identity_negative_keys(issuer,kid,expires_at) SELECT %s,'missing-'||n,"
            "clock_timestamp()+interval '60 seconds' FROM generate_series(1,128) n",(ISSUER,))
        await self.execute("UPDATE {s}.identity_jwks SET attempted_at=clock_timestamp()-interval '61 seconds'")
        result=await self.repo.call("jwks_reserve",{"issuer":ISSUER,"kid":"another-missing","lease_hash":lease})
        require(result["refresh"] is True)
        require((await self.repo.call("jwks_publish",{"issuer":ISSUER,"kid":"another-missing","lease_hash":lease,
                                                     "keys":[self.fixture.key]}))["status"]=="ok")
        require((await self.admin("SELECT count(*) FROM {s}.identity_negative_keys"))[0][0]==128)
        completed.append("negative_key_cache_capacity")

        await self.reset()
        async with self.client() as client:
            params=await self.start(client)
            async with direct_connection(self.direct) as connection:
                async with connection.transaction():
                    await connection.execute(sql.SQL("WITH stamp AS(SELECT clock_timestamp() t) UPDATE {s}.login_transactions "
                        "SET created_at=stamp.t-interval '299.85 seconds',expires_at=stamp.t+interval '0.15 seconds' FROM stamp").format(s=sql.Identifier(self.schema)))
                    pending=asyncio.create_task(self.callback(client,params))
                    await asyncio.sleep(0.3)
                    require(not pending.done())
                require((await pending).status_code==401 and self.fixture.tokens==0)
        completed.append("claim_expiry_while_contended")

        await self.reset()
        async with self.client() as client:
            identity=await self.signed_in(client)
            async with direct_connection(self.direct) as connection:
                async with connection.transaction():
                    await connection.execute(sql.SQL("WITH stamp AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET "
                        "authenticated_at=stamp.t-interval '8 hours'+interval '0.15 seconds',"
                        "created_at=stamp.t-interval '8 hours'+interval '0.15 seconds',"
                        "expires_at=stamp.t+interval '0.15 seconds',last_seen_at=stamp.t-interval '1 second' FROM stamp").format(s=sql.Identifier(self.schema)))
                    pending=asyncio.create_task(client.get("/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"}))
                    await asyncio.sleep(0.3)
                    require(not pending.done())
                require((await pending).status_code==401)
        completed.append("session_expiry_while_contended")

        await self.reset()
        async with self.client() as client:
            identity=await self.signed_in(client)
            old=client.cookies.get(SESSION)
            params=await self.start(client)
            async with self.client(cookies={SESSION:old}) as parallel:
                async with direct_connection(self.direct) as connection:
                    async with connection.transaction():
                        await connection.execute(sql.SQL("SELECT id FROM {s}.principals WHERE id=%s FOR UPDATE").format(s=sql.Identifier(self.schema)),(identity["principal_id"],))
                        callback=asyncio.create_task(self.callback(client,params))
                        await asyncio.sleep(0.3)
                        lookup=asyncio.create_task(parallel.get("/api/v1/me",headers={"X-EAI-CSRF-Bootstrap":"1","Sec-Fetch-Site":"same-origin"}))
                        await asyncio.sleep(0.3)
                    responses=await asyncio.gather(callback,lookup)
                    require(responses[0].status_code==303 and responses[1].status_code in {200,401})
        completed.append("principal_before_session_lock_order")

        for locked in ("principal","old_session"):
            await self.reset()
            async with self.client() as client:
                identity=await self.signed_in(client)
                params=await self.start(client)
                old_token=client.cookies.get(SESSION)
                async with direct_connection(self.direct) as connection:
                    async with connection.transaction():
                        if locked=="principal":
                            await connection.execute(sql.SQL("SELECT id FROM {s}.principals WHERE id=%s FOR UPDATE").format(s=sql.Identifier(self.schema)),(identity["principal_id"],))
                        else:
                            await connection.execute(sql.SQL("SELECT id FROM {s}.sessions WHERE token_hash=%s FOR UPDATE").format(s=sql.Identifier(self.schema)),(digest("session",old_token),))
                        await self.execute("WITH stamp AS(SELECT clock_timestamp() t) UPDATE {s}.login_transactions "
                            "SET created_at=stamp.t-interval '299 seconds',expires_at=stamp.t+interval '1 second' FROM stamp")
                        pending=asyncio.create_task(self.callback(client,params))
                        await asyncio.sleep(1.2)
                        if pending.done():
                            raise VerificationFailure("Expiry contention finished early: status="+str(pending.result().status_code)+", token_calls="+str(self.fixture.tokens))
                    require((await pending).status_code==401)
                    require((await self.admin("SELECT count(*) FROM {s}.sessions WHERE revoked_at IS NULL"))[0][0]==1)
            completed.append("complete_expiry_after_"+locked+"_lock")

        for boundary in ("absolute","idle"):
            await self.reset()
            async with self.client() as client:
                identity=await self.signed_in(client)
                tenant,membership=await self.memberships(identity["principal_id"])
                async with direct_connection(self.direct) as connection:
                    async with connection.transaction():
                        await connection.execute(sql.SQL("SELECT id FROM {s}.memberships WHERE id=%s FOR UPDATE").format(s=sql.Identifier(self.schema)),(membership,))
                        if boundary=="absolute":
                            await self.execute("WITH stamp AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET "
                                "authenticated_at=stamp.t-interval '8 hours'+interval '1 second',created_at=stamp.t-interval '8 hours'+interval '1 second',"
                                "expires_at=stamp.t+interval '1 second',last_seen_at=stamp.t FROM stamp")
                        else:
                            await self.execute("WITH stamp AS(SELECT clock_timestamp() t) UPDATE {s}.sessions SET "
                                "authenticated_at=stamp.t-interval '1 hour',created_at=stamp.t-interval '1 hour',"
                                "expires_at=stamp.t+interval '7 hours',last_seen_at=stamp.t-interval '1799 seconds' FROM stamp")
                        pending=asyncio.create_task(client.post("/api/v1/session/tenant",headers=self.headers(identity["csrf_token"]),json={"membership_id":membership}))
                        await asyncio.sleep(1.2)
                        if pending.done():
                            raise VerificationFailure("Expiry contention finished early: status="+str(pending.result().status_code)+", token_calls="+str(self.fixture.tokens))
                    require((await pending).status_code==401)
                    require((await self.admin("SELECT count(*) FROM {s}.sessions"))[0][0]==1)
            completed.append("switch_"+boundary+"_expiry_after_membership_lock")

        await self.reset()
        async with self.client() as client:
            identity=await self.signed_in(client)
            tenant,membership=await self.memberships(identity["principal_id"])
            switched=await client.post("/api/v1/session/tenant",headers=self.headers(identity["csrf_token"]),json={"membership_id":membership})
            require(switched.status_code==200)
            old_token=client.cookies.get(SESSION);old_csrf=switched.json()["csrf_token"]
            await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) "
                "VALUES(%s,%s,%s,'employee','tenant','self',%s)",(tenant,uuid4(),membership,identity["principal_id"]))
            response=await client.post("/api/v1/auth/logout",headers=self.headers(old_csrf),json={})
            require(response.status_code==401)
            require((await self.admin("SELECT revoked_at IS NOT NULL FROM {s}.sessions WHERE token_hash=%s",(digest("session",old_token),)))[0][0])
            fresh=await self.signed_in(client)
            require(client.cookies.get(SESSION)!=old_token and fresh["csrf_token"]!=old_csrf and fresh["active_tenant_id"] is None)
        completed.append("privilege_change_revokes_and_reauth_rotates")

        for table in ("sessions","login_transactions","identity_limits","identity_jwks","identity_negative_keys"):
            try:
                async with self.database.transaction() as connection:
                    await connection.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(self.schema),sql.Identifier(table)))
            except Exception:pass
            else:raise VerificationFailure()
        require(await self.database.ping())
        completed.append("identity_tables_not_directly_readable")
        return completed


async def verify(direct, base, fixture_file, case_id=None, extra_only=False):
    schema = "db03_probe_"+uuid4().hex
    database = None
    owned = False
    stage = "namespace"
    checks=[]
    try:
        async with direct_connection(direct) as connection:
            require(await (await connection.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s",(schema,))).fetchone() is None)
        owned=True
        stage="migration"
        await migrate(direct,upgrade=True,schema=schema)
        target=await provision_role(direct,base,schema=schema,role_name="eai_runtime_"+schema.rsplit("_",1)[1])
        database=RuntimeDatabase(target,schema=schema)
        await database.open()
        harness=Harness(direct,database,schema,target)
        fixture=json.loads(fixture_file.read_text(encoding="utf-8"))
        require([c["id"] for c in fixture["cases"]]==[f"SEC-06-{i:03}" for i in range(1,87)])
        for case in fixture["cases"]:
            if extra_only:continue
            if case_id and case["id"] != case_id:continue
            stage=case["id"]
            await harness.case(case)
            checks.append(stage)
            if len(checks)%10==0:print(f"Identity cases checked: {len(checks)}",flush=True)
        stage="QA-regressions"
        extra=await harness.regressions() if not case_id else []
        return {"passed":True,"scope":"single_case" if case_id else "qa_regressions" if extra_only else "sec06_plus_qa",
                "fixture_version":fixture["contract_version"],"sec06_cases":checks,"qa_regressions":extra,"actual_provider":"controlled_signed_fixture",
                "real_postgres_restricted_runtime":True,"disposable_cleanup":"completed"}
    except Exception as error:
        detail=str(error) if isinstance(error,VerificationFailure) else "sanitized operation failure"
        raise VerificationFailure("Identity verification failed at "+stage+": "+detail) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,default=Path(".env"))
    parser.add_argument("--fixture",type=Path,default=Path("backend/tests/contracts/identity_protocol_cases.json"))
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--case", choices=[f"SEC-06-{i:03}" for i in range(1,87)])
    parser.add_argument("--extra-only", action="store_true")
    parser.add_argument("--output",type=Path,help="Write sanitized evidence after successful cleanup.")
    args=parser.parse_args()
    if not args.live:parser.error("Explicit --live is required for the dedicated database check.")
    safe_transport_logs()
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text('{"passed":false,"status":"running"}\n',encoding="utf-8")
        except Exception:
            print("Cannot invalidate prior evidence; no database checks started.",file=sys.stderr)
            raise SystemExit(1) from None
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value())
        direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
        verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,args.fixture,args.case,args.extra_only))
        print(json.dumps(result,sort_keys=True))
        if args.output is not None:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    except VerificationFailure as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Identity verification unavailable; inspect sanitized diagnostics only.",file=sys.stderr)
        raise SystemExit(1) from None


if __name__=="__main__":main()
