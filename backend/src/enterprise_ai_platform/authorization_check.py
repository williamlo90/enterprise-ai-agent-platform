"""AUTH-03 explicit PostgreSQL check using fictional actors and disposable feature fixtures."""

import argparse
import asyncio
from dataclasses import replace
import inspect
import json
from pathlib import Path
import secrets
import sys
from uuid import UUID, uuid4
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, direct_connection, parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate, access_role
from enterprise_ai_platform.infrastructure.database.roles import provision_role, drop_probe_schema
from enterprise_ai_platform.infrastructure.database.authorization import PostgresAuthorizationRepository
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.modules.authorization.domain import Subject, RoleGrant, Resource, ResourceRef, ACLGrant, Action
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.modules.authorization.service import AuthorizationService
from enterprise_ai_platform.modules.identity.service import digest

ORIGIN = "https://access-fixture.example.test"


def fixture_subject(data):
    return Subject(UUID(data["tenant_id"]), UUID(data["principal_id"]), UUID(data["membership_id"]),
                   data["access_revision"], tuple(RoleGrant(**g) for g in data["grants"]))


def fixture_resource(data):
    if data is None:
        return None
    values = dict(data)
    ref = values.pop("ref")
    for key in ("owner_id", "collection_id", "active_reviewer_id"):
        if values.get(key):
            values[key] = UUID(values[key])
    acl = values.pop("acl", [])
    values["acl"] = tuple(ACLGrant(g["permission"], UUID(g["membership_id"]) if g.get("membership_id") else None,
                                   RoleGrant(**g["role"]) if g.get("role") else None) for g in acl)
    return Resource(ResourceRef(UUID(ref["tenant_id"]), ref["kind"], UUID(ref["object_id"])), **values)


class CheckFailure(RuntimeError):
    pass


def require(value, name):
    if not value:
        raise CheckFailure(name)


class DiagnosticRepository(PostgresAuthorizationRepository):
    diagnostic="none"
    async def _call(self,*args,**kwargs):
        try:return await super()._call(*args,**kwargs)
        except Exception as error:
            code=getattr(error,"sqlstate",None)
            self.diagnostic=code if isinstance(code,str) and len(code)==5 and code.isalnum() else type(error).__name__
            raise


class Harness:
    def __init__(self, admin, database, schema):
        self.admin, self.database, self.schema = admin, database, schema
        self.repo = DiagnosticRepository(database, resource_loader=self.load,resource_writer=self.write)
        self.service = AuthorizationService(self.repo, ORIGIN)
        self.issuer = "https://identity-fixture.example.test"

    async def execute(self, query, params=None):
        try:
            return await self.admin.execute(sql.SQL(query).format(s=sql.Identifier(self.schema), a=sql.Identifier(access_role(self.schema))), params)
        except Exception as error:
            code=getattr(error,"sqlstate",None)
            raise CheckFailure("fixture-line-"+str(inspect.currentframe().f_back.f_lineno)+"-"+(code if isinstance(code,str) and code.isalnum() and len(code)==5 else type(error).__name__)) from None

    async def setup(self):
        await self.execute("CREATE TABLE {s}.authorization_probe_objects(tenant_id uuid,id uuid,kind text,facts jsonb,PRIMARY KEY(tenant_id,id,kind))")
        await self.execute("ALTER TABLE {s}.authorization_probe_objects ENABLE ROW LEVEL SECURITY; ALTER TABLE {s}.authorization_probe_objects FORCE ROW LEVEL SECURITY")
        await self.execute("CREATE POLICY maintenance ON {s}.authorization_probe_objects TO CURRENT_USER USING(true) WITH CHECK(true)")
        await self.execute("CREATE POLICY fixture_scope ON {s}.authorization_probe_objects FOR SELECT TO {a} USING({s}.has_active_context(tenant_id))")
        await self.execute("GRANT SELECT,UPDATE(facts) ON {s}.authorization_probe_objects TO {a}")
        await self.execute("CREATE POLICY fixture_write ON {s}.authorization_probe_objects FOR UPDATE TO {a} USING({s}.has_active_context(tenant_id)) WITH CHECK({s}.has_active_context(tenant_id))")

    async def load(self, connection, subject, ref):
        cursor = await connection.execute(sql.SQL("SELECT facts FROM {}.authorization_probe_objects WHERE tenant_id=%s AND id=%s AND kind=%s").format(sql.Identifier(self.schema)), (ref.tenant_id, ref.object_id, ref.kind))
        row = await cursor.fetchone()
        return fixture_resource(row[0]) if row else None

    async def write(self, connection, subject, resource, action, command):
        if command not in ({"edit":True},{"withdraw":True}):raise AccessFailure(400)
        cursor=await connection.execute(sql.SQL("UPDATE {}.authorization_probe_objects SET facts=jsonb_set(facts,'{{version}}',to_jsonb(%s::bigint)) || %s WHERE tenant_id=%s AND id=%s AND kind=%s AND (facts->>'version')::bigint=%s AND (facts->>'access_revision')::bigint=%s RETURNING facts").format(sql.Identifier(self.schema)),
            (resource.version+1,Jsonb({"withdrawn":True} if command.get("withdraw") else {}),resource.ref.tenant_id,resource.ref.object_id,resource.ref.kind,resource.version,resource.access_revision))
        row=await cursor.fetchone()
        if row is None:raise AccessFailure(409)
        return fixture_resource(row[0])

    async def tenant(self, tenant):
        if (await (await self.execute("SELECT 1 FROM {s}.tenants WHERE id=%s", (tenant,))).fetchone()):
            return
        async with self.admin.transaction():
            await self.execute("INSERT INTO {s}.tenants(id,slug,name,timezone) VALUES(%s,%s,'Fictional tenant','UTC')", (tenant,"access-"+tenant.hex))
            await self.execute("INSERT INTO {s}.tenant_configs(tenant_id,id,config_version,assistant_name,locales,help_contact,instruction_text,connector_ref,model_policy_ref,schema_version) VALUES(%s,%s,1,'Fixture','[\"en\"]','Fixture','Fixture','fixture','fixture',1)", (tenant,uuid4()))

    async def member(self, tenant, person, member=None, grants=()):
        await self.tenant(tenant)
        await self.execute("INSERT INTO {s}.principals(id,issuer,subject) VALUES(%s,%s,%s) ON CONFLICT(id) DO NOTHING", (person,self.issuer,"fictional-"+person.hex))
        row = await (await self.execute("SELECT id FROM {s}.memberships WHERE tenant_id=%s AND principal_id=%s", (tenant,person))).fetchone()
        member = row[0] if row else member or uuid4()
        if not row:
            await self.execute("INSERT INTO {s}.memberships(tenant_id,id,principal_id) VALUES(%s,%s,%s)", (tenant,member,person))
        await self.execute("DELETE FROM {s}.membership_roles WHERE tenant_id=%s AND membership_id=%s", (tenant,member))
        for grant in grants:
            if grant.role not in {"employee","knowledge_manager","tenant_admin","process_owner","approver","operator_scoped"}:
                continue  # Unknown roles are blocked by the foundation schema itself.
            await self.execute("INSERT INTO {s}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                               (tenant,uuid4(),member,grant.role,grant.scope_kind,grant.scope_ref,person))
        return member

    async def session(self, tenant, person):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        await self.execute("INSERT INTO {s}.sessions(id,principal_id,active_tenant_id,membership_revision,token_hash,csrf_hash,expires_at) SELECT %s,%s,%s,access_revision,%s,%s,CURRENT_TIMESTAMP+interval '8 hours' FROM {s}.memberships WHERE tenant_id=%s AND principal_id=%s",
                           (uuid4(),person,tenant,digest("session",token),digest("csrf",csrf),tenant,person))
        return token,csrf

    def headers(self, csrf):
        return {"origin":ORIGIN,"content-type":"application/json","x-csrf-token":csrf}

    async def matrix(self, cases):
        checked=[]
        for case in cases:
            actor=fixture_subject(case["subject"])
            await self.member(actor.tenant_id,actor.principal_id,actor.membership_id,actor.grants)
            for grant in actor.grants:
                if grant.role=="knowledge_manager" and grant.scope_kind=="knowledge_collection" and grant.valid(actor.tenant_id):
                    await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s) ON CONFLICT DO NOTHING",(actor.tenant_id,UUID(grant.scope_ref)))
            await self.execute("DELETE FROM {s}.authorization_probe_objects")
            resource=fixture_resource(case["resource"])
            ref=resource.ref if resource else ResourceRef(actor.tenant_id,"document",uuid4())
            if resource:
                await self.tenant(ref.tenant_id)
                await self.execute("INSERT INTO {s}.authorization_probe_objects VALUES(%s,%s,%s,%s)",(ref.tenant_id,ref.object_id,ref.kind,Jsonb(case["resource"])))
            token,csrf=await self.session(actor.tenant_id,actor.principal_id)
            status=0
            action=Action(case["action"]) if case["action"] in Action else case["action"]
            try:
                async with self.service.access(token,ref,action,changed_fields=frozenset(case["changed_fields"]),headers=self.headers(csrf)):
                    pass
            except AccessFailure as error:
                status=error.status
            require(status==case["expected_status"],case["id"]+"-status-"+str(status))
            unchanged=(await (await self.execute("SELECT last_seen_at=authenticated_at FROM {s}.sessions WHERE token_hash=%s",(digest("session",token),))).fetchone())[0]
            require(unchanged==(status!=0),case["id"]+"-idle")
            checked.append(case["id"])
            if len(checked)%20==0:print("Authorization matrix checked: "+str(len(checked)),flush=True)
        return checked

    async def regressions(self):
        self.regression="start"
        checked=[];tenant,other,actor,recipient=uuid4(),uuid4(),uuid4(),uuid4()
        admin=RoleGrant("tenant_admin","tenant",str(tenant))
        employee=RoleGrant("employee","tenant",str(tenant))
        own=await self.member(tenant,actor,grants=(admin,employee))
        target=await self.member(tenant,recipient,grants=(employee,))
        token,csrf=await self.session(tenant,actor)
        async def version():return (await (await self.execute("SELECT version FROM {s}.memberships WHERE tenant_id=%s AND id=%s",(tenant,target))).fetchone())[0]
        async def command(**changes):
            return {"operation":"grant","membership_id":str(target),"expected_version":await version(),"role":"approver","scope_kind":"process","scope_ref":"reimbursement","idempotency_key":str(uuid4()),**changes}
        async def denied(cmd,status, *, session=token, secret=csrf):
            try:await self.service.change_role(session,self.headers(secret),cmd)
            except AccessFailure as error:require(error.status==status,"role-denial-"+str(error.status))
            else:raise CheckFailure("role-denial-missing")
        await denied(await command(membership_id=str(own)),403);checked.append("global_principal_self_grant_denied");self.regression=checked[-1]
        await denied(await command(),403,secret="z"*43);checked.append("csrf_denied_without_write");self.regression=checked[-1]
        foreign=await self.member(other,uuid4())
        await denied(await command(membership_id=str(foreign)),404);checked.append("foreign_membership_hidden");self.regression=checked[-1]
        await denied(await command(scope_ref="*"),400);checked.append("wildcard_scope_denied");self.regression=checked[-1]
        await denied(await command(role="knowledge_manager",scope_kind="knowledge_collection",scope_ref=str(uuid4())),400);checked.append("unregistered_collection_denied");self.regression=checked[-1]
        collection=uuid4()
        await self.execute("INSERT INTO {s}.authorization_scopes VALUES(%s,'knowledge_collection',%s,true)",(other,collection))
        await denied(await command(role="knowledge_manager",scope_kind="knowledge_collection",scope_ref=str(collection)),400);checked.append("foreign_collection_denied");self.regression=checked[-1]
        original=await command()
        result=await self.service.change_role(token,self.headers(csrf),original)
        require(result["changed"] and not result["replayed"],"grant-commit")
        require((await self.service.change_role(token,self.headers(csrf),original))["replayed"],"idempotent-replay")
        await denied(dict(original,role="process_owner"),409)
        require((await (await self.execute("SELECT count(*) FROM {s}.role_change_events WHERE tenant_id=%s",(tenant,))).fetchone())[0]==1,"audit-exactly-once")
        checked.append("grant_revision_idempotency_audit_atomic");self.regression=checked[-1]
        stale,_=await self.session(tenant,recipient)
        revoke=await command(operation="revoke")
        require((await self.service.change_role(token,self.headers(csrf),revoke))["changed"],"revoke-commit")
        try:
            async with self.repo.scope(digest("session",stale)):pass
        except AccessFailure as error:require(error.status==401,"stale-session-denied")
        else:raise CheckFailure("stale-session-accepted")
        checked.append("role_revoke_invalidates_recipient_session");self.regression=checked[-1]
        # A scope can stop being active without becoming impossible to revoke.
        collection=uuid4()
        await self.execute("INSERT INTO {s}.authorization_scopes VALUES(%s,'knowledge_collection',%s,true)",(tenant,collection))
        scoped=await command(role="knowledge_manager",scope_kind="knowledge_collection",scope_ref=str(collection))
        await self.service.change_role(token,self.headers(csrf),scoped)
        await self.execute("UPDATE {s}.authorization_scopes SET active=false WHERE scope_ref=%s",(collection,))
        await self.service.change_role(token,self.headers(csrf),await command(operation="revoke",role="knowledge_manager",scope_kind="knowledge_collection",scope_ref=str(collection)))
        await self.execute("UPDATE {s}.authorization_scopes SET active=true WHERE scope_ref=%s",(collection,))
        recipient_token,_=await self.session(tenant,recipient)
        async with self.repo.scope(digest("session",recipient_token)) as unit:
            require(not unit.subject.has("knowledge_manager","knowledge_collection",str(collection)),"inactive-scope-revoke-remains-removed")
        checked.append("deactivate_revoke_reactivate_does_not_restore_grant");self.regression=checked[-1]
        # The same actor/session serializes correctly and an idempotent replay is not a second write.
        concurrent=await command(role="process_owner")
        outcomes=await asyncio.gather(*(self.service.change_role(token,self.headers(csrf),concurrent) for _ in range(2)))
        require(sum(not x["replayed"] for x in outcomes)==1,"parallel-idempotency")
        checked.append("concurrent_same_key_one_audit_outcome");self.regression=checked[-1]
        first=await command(role="approver");second=await command(role="knowledge_manager",scope_kind="tenant",scope_ref=str(tenant),expected_version=first["expected_version"])
        outcomes=await asyncio.gather(self.service.change_role(token,self.headers(csrf),first),self.service.change_role(token,self.headers(csrf),second),return_exceptions=True)
        require(sum(isinstance(x,dict) for x in outcomes)==1 and any(isinstance(x,AccessFailure) and x.status==409 for x in outcomes),"parallel-version-conflict")
        checked.append("concurrent_version_one_winner");self.regression=checked[-1]
        # Real feature fixture writes use the same connection/transaction as the policy guard.
        editable=ResourceRef(tenant,"draft",uuid4())
        facts={"ref":{"tenant_id":str(tenant),"kind":"draft","object_id":str(editable.object_id)},"version":1,"access_revision":1,"owner_id":str(actor)}
        await self.execute("INSERT INTO {s}.authorization_probe_objects VALUES(%s,%s,'draft',%s)",(tenant,editable.object_id,Jsonb(facts)))
        async with self.service.access(token,editable,Action.WRITE,headers=self.headers(csrf)) as permit:
            require((await permit.apply({"edit":True})).version==2,"atomic-write-version")
        checked.append("versioned_write_commits_in_access_transaction");self.regression=checked[-1]
        try:
            async with self.service.access(token,editable,Action.WRITE,headers=self.headers(csrf)) as permit:
                await permit.apply({"edit":True})
                raise AccessFailure(409)
        except AccessFailure:pass
        require((await (await self.execute("SELECT (facts->>'version')::int FROM {s}.authorization_probe_objects WHERE id=%s",(editable.object_id,))).fetchone())[0]==2,"write-rollback")
        checked.append("failure_after_write_rolls_back_feature_and_activity");self.regression=checked[-1]
        try:
            async with self.service.access(token,editable,Action.READ):
                await self.execute("UPDATE {s}.authorization_probe_objects SET facts=jsonb_set(facts,'{{owner_id}}',%s) WHERE id=%s",(Jsonb(str(recipient)),editable.object_id))
        except AccessFailure as error:require(error.status==404,"object-revoke-before-output")
        else:raise CheckFailure("object-revoke-not-detected")
        checked.append("current_object_owner_rechecked_before_output");self.regression=checked[-1]
        await self.execute("UPDATE {s}.authorization_probe_objects SET facts=jsonb_set(facts,'{{owner_id}}',%s) WHERE id=%s",(Jsonb(str(actor)),editable.object_id))
        expiring,expiring_csrf=await self.session(tenant,actor)
        await self.execute("WITH expiry AS (SELECT clock_timestamp()+interval '2 seconds' t) UPDATE {s}.sessions SET authenticated_at=expiry.t-interval '8 hours',expires_at=expiry.t FROM expiry WHERE token_hash=%s",(digest("session",expiring),))
        try:
            async with self.service.access(expiring,editable,Action.WRITE,headers=self.headers(expiring_csrf)) as permit:
                await asyncio.sleep(2.1)
                await permit.apply({"edit":True})
        except AccessFailure as error:require(error.status==401,"expiry-before-write")
        else:raise CheckFailure("expired-write-not-denied")
        require((await (await self.execute("SELECT (facts->>'version')::int FROM {s}.authorization_probe_objects WHERE id=%s",(editable.object_id,))).fetchone())[0]==2,"expired-write-absent")
        checked.append("original_session_expiry_rechecked_before_write");self.regression=checked[-1]
        await self.execute("DELETE FROM {s}.membership_roles WHERE tenant_id=%s AND membership_id=%s AND role='tenant_admin'",(tenant,own))
        fresh,new_csrf=await self.session(tenant,actor)
        await denied(original,403,session=fresh,secret=new_csrf);checked.append("replay_reauthorizes_current_grantor");self.regression=checked[-1]
        for table in ("role_change_events","authorization_scopes"):
            try:
                async with self.database.transaction() as connection:
                    await connection.execute(sql.SQL("SELECT * FROM {}.{}").format(sql.Identifier(self.schema),sql.Identifier(table)))
            except Exception:pass
            else:raise CheckFailure("private-table-readable")
        checked.append("private_tables_denied_to_runtime");self.regression=checked[-1]
        require(await self.database.ping(),"pool-reuse-after-denial")
        return checked


async def verify(direct,base,fixture,regressions_only=False):
    schema="db03_probe_"+uuid4().hex;database=None;owned=False;stage="namespace"
    try:
        async with direct_connection(direct) as connection:
            require((await (await connection.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s",(schema,))).fetchone()) is None,"namespace")
        owned=True;stage="migration"
        await migrate(direct,upgrade=True,schema=schema)
        stage="restricted-role"
        target=await provision_role(direct,base,schema=schema,role_name="eai_runtime_"+schema.rsplit("_",1)[1])
        database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            harness=Harness(connection,database,schema);await harness.setup()
            stage="matrix";cases=[] if regressions_only else await harness.matrix(fixture["cases"])
            stage="regressions";regressions=await harness.regressions()
        return {"passed":True,"scope":"regressions" if regressions_only else "matrix_and_regressions","fixture_version":fixture["contract_version"],"matrix_cases":cases,"regressions":regressions,
                "real_postgres_restricted_runtime":True,"resource_loader":"disposable_feature_fixture",
                "actual_provider_login":"not_repeated_AUTH02_prerequisite","disposable_cleanup":"completed"}
    except Exception as error:
        detail=str(error) if isinstance(error,CheckFailure) else type(error).__name__+"-"+str(getattr(error,"status",""))
        if stage=="regressions":detail+="-after-"+harness.regression+"-sql-"+harness.repo.diagnostic
        raise CheckFailure(stage+":"+detail) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regressions-only",action="store_true");parser.add_argument("--live",action="store_true");parser.add_argument("--env-file",type=Path,default=Path(".env"))
    parser.add_argument("--output",type=Path,default=Path("local/auth03/contract-live.json"));args=parser.parse_args()
    if not args.live:parser.error("Explicit --live is required.")
    safe_transport_logs()
    try:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text('{"passed":false,"status":"running"}',encoding="utf-8")
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
        verify_same_database(base,direct)
        fixture=json.loads(Path("backend/tests/contracts/authorization_cases.json").read_text(encoding="utf-8"))
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,fixture,args.regressions_only))
        args.output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8");print(json.dumps(result,sort_keys=True))
    except Exception as error:
        print("Authorization verification failed: "+(str(error) if isinstance(error,CheckFailure) else "sanitized_operation_failure"),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=="__main__":main()
