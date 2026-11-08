import asyncio
from functools import wraps
import json
from pathlib import Path
from dataclasses import replace
from contextlib import asynccontextmanager
from uuid import UUID, uuid4
import pytest
from enterprise_ai_platform.modules.authorization.domain import Action, Subject, Resource, ResourceRef, ACLGrant, RoleGrant, evaluate
from enterprise_ai_platform.modules.authorization.service import AuthorizationService
from enterprise_ai_platform.modules.authorization.ports import AccessFailure

FIXTURE = json.loads((Path(__file__).parents[1]/"contracts/authorization_cases.json").read_text(encoding="utf-8"))


from enterprise_ai_platform.authorization_check import fixture_subject as subject, fixture_resource as resource


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c:c["id"]+"-"+c["name"])
def test_contract_matrix(case):
    action = Action(case["action"]) if case["action"] in Action else case["action"]
    assert evaluate(subject(case["subject"]), resource(case["resource"]), action,
                    changed_fields=frozenset(case["changed_fields"])) == case["expected_status"]


def test_acl_management_does_not_delegate_publish_or_acl_administration():
    case=next(c for c in FIXTURE["cases"] if c["name"]=="explicit member ACL reads")
    actor=subject(case["subject"]);doc=resource(case["resource"])
    doc=replace(doc,acl=(ACLGrant("manage",membership_id=actor.membership_id),))
    assert evaluate(actor,doc,Action.WRITE)==0
    assert evaluate(actor,doc,Action.MANAGE_ACL)==403
    assert evaluate(actor,doc,Action.PUBLISH)==403


def run_async(function):
    @wraps(function)
    def wrapped(*args,**kwargs):return asyncio.run(function(*args,**kwargs))
    return wrapped


class Repository:
    def __init__(self, actor, obj):
        self.subject, self.obj = actor,obj
        self.closed=False;self.committed=False;self.role_calls=[]
    @asynccontextmanager
    async def scope(self, token_hash, *, csrf_hash=None):
        assert len(token_hash)==64
        try:
            yield self
            self.committed=True
        finally:self.closed=True
    async def resource(self, ref):return self.obj
    async def validate(self):pass
    async def write(self, resource, action, command):
        self.obj=replace(resource,version=resource.version+1)
        return self.obj
    async def change_role(self,*args):self.role_calls.append(args);return {"changed":True}


@run_async
@pytest.mark.parametrize("change",["withdraw", "owner", "version", "acl"])
async def test_recheck_blocks_mid_request_object_changes(change):
    case=next(c for c in FIXTURE["cases"] if c["name"]=="explicit member ACL reads")
    actor,obj=subject(case["subject"]),resource(case["resource"])
    repo=Repository(actor,obj);service=AuthorizationService(repo,"https://fixture.example.test")
    with pytest.raises(AccessFailure) as error:
        async with service.access("s"*43,obj.ref,Action.READ):
            repo.obj=replace(obj,**{"withdrawn":True}) if change=="withdraw" else replace(obj,acl=()) if change=="acl" else replace(obj,version=2) if change=="version" else replace(obj,owner_id=uuid4())
    assert error.value.status in {404,409}
    assert repo.closed and not repo.committed


@run_async
async def test_foreign_tenant_rejected_before_resource_lookup():
    actor=subject(FIXTURE["cases"][0]["subject"])
    repo=Repository(actor,None);service=AuthorizationService(repo,"https://fixture.example.test")
    async def forbidden(*_):pytest.fail("Do not look up foreign resources")
    repo.resource=forbidden
    with pytest.raises(AccessFailure) as error:
        async with service.access("s"*43,ResourceRef(uuid4(),"document",uuid4()),Action.READ):pass
    assert error.value.status==404


@run_async
@pytest.mark.parametrize("bad",["origin","csrf","fields","version","operator","uuid"])
async def test_role_change_boundary_rejects_forged_inputs_before_repository(bad):
    actor=subject(FIXTURE["cases"][0]["subject"]);repo=Repository(actor,None)
    service=AuthorizationService(repo,"https://fixture.example.test")
    headers={"origin":"https://fixture.example.test","content-type":"application/json","x-csrf-token":"c"*43}
    command={"operation":"grant","membership_id":str(uuid4()),"expected_version":1,"role":"approver",
             "scope_kind":"process","scope_ref":"reimbursement","idempotency_key":str(uuid4())}
    if bad=="origin":headers["origin"]="https://foreign.example.test"
    if bad=="csrf":headers["x-csrf-token"]="invalid"
    if bad=="fields":command["actor_id"]=str(actor.principal_id)
    if bad=="version":command["expected_version"]=True
    if bad=="operator":command["role"]="operator_scoped"
    if bad=="uuid":command["membership_id"]="guessed"
    with pytest.raises(AccessFailure):await service.change_role("s"*43,headers,command)
    assert not repo.role_calls


@run_async
async def test_missing_browser_session_has_no_bearer_or_role_fallback():
    repo=Repository(None,None);service=AuthorizationService(repo,"https://fixture.example.test")
    with pytest.raises(AccessFailure) as error:
        async with service.access(None,ResourceRef(uuid4(),"document",uuid4()),Action.READ):pass
    assert error.value.status==401 and not repo.closed


@run_async
async def test_recorded_versioned_write_is_accepted_but_read_receipt_is_rejected():
    case=next(c for c in FIXTURE["cases"] if c["name"]=="owner edits draft")
    actor,obj=subject(case["subject"]),resource(case["resource"])
    repo=Repository(actor,obj);service=AuthorizationService(repo,"https://fixture.example.test")
    async with service.access("s"*43,obj.ref,Action.WRITE,headers={"origin":"https://fixture.example.test","content-type":"application/json","x-csrf-token":"c"*43}) as permit:
        await permit.apply({"fixture":"edit"})
    assert repo.committed
    repo=Repository(actor,obj);service=AuthorizationService(repo,"https://fixture.example.test")
    with pytest.raises(ValueError):
        async with service.access("s"*43,obj.ref,Action.READ) as permit:
            await permit.apply({"fixture":"edit"})
    assert not repo.committed


@run_async
async def test_http_guard_ignores_forged_identity_headers_and_uses_unique_cookie():
    import httpx
    from enterprise_ai_platform.api.access import authorized_resource
    from enterprise_ai_platform.main import create_app
    from enterprise_ai_platform.config import Settings
    from enterprise_ai_platform.composition import ApplicationServices
    from fastapi import Request
    case=next(c for c in FIXTURE["cases"] if c["name"]=="owner edits draft")
    actor,obj=subject(case["subject"]),resource(case["resource"])
    repo=Repository(actor,obj);service=AuthorizationService(repo,"https://fixture.example.test")
    app=create_app(Settings(env="test",host="127.0.0.1",port=8000),services=ApplicationServices(health=None,authorization=service))
    @app.get("/fixture")
    async def route(request:Request):
        async with authorized_resource(request,obj.ref,Action.READ):pass
        return {"allowed":True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://fixture.example.test") as client:
        denied=await client.get("/fixture",headers={"Authorization":"Bearer fake","X-Actor-ID":str(actor.principal_id),"X-Role":"tenant_admin"})
        assert denied.status_code==401 and denied.headers["cache-control"]=="no-store"
        assert denied.json()["error"]["code"]=="unauthenticated"
        accepted=await client.get("/fixture",headers={"Cookie":"__Host-eai-session="+"s"*43})
        assert accepted.status_code==200 and accepted.headers["cache-control"]=="no-store"
        duplicate=await client.get("/fixture",headers={"Cookie":"__Host-eai-session="+"s"*43+"; __Host-eai-session="+"t"*43})
        assert duplicate.status_code==401


def test_active_reviewer_does_not_implicitly_read_trip_and_receipt_requires_bound_revision():
    case=next(c for c in FIXTURE["cases"] if c["name"]=="assigned reviewer reads")
    actor,obj=subject(case["subject"]),resource(case["resource"])
    trip=replace(obj,ref=replace(obj.ref,kind="trip"))
    receipt=replace(obj,ref=replace(obj.ref,kind="receipt"))
    assert evaluate(actor,trip,Action.READ)==404
    assert evaluate(actor,receipt,Action.READ)==404
    assert evaluate(actor,replace(receipt,receipt_revision_bound=True),Action.READ)==0


@run_async
async def test_mutation_needs_same_origin_csrf_before_repository():
    case=next(c for c in FIXTURE["cases"] if c["name"]=="owner edits draft")
    actor,obj=subject(case["subject"]),resource(case["resource"])
    repo=Repository(actor,obj);service=AuthorizationService(repo,"https://fixture.example.test")
    with pytest.raises(AccessFailure) as error:
        async with service.access("s"*43,obj.ref,Action.WRITE):pass
    assert error.value.status==403 and not repo.closed


@run_async
async def test_adapter_rejects_wrong_reference_and_closed_units():
    from enterprise_ai_platform.infrastructure.database.authorization import AccessUnit
    case=next(c for c in FIXTURE["cases"] if c["name"]=="owner edits draft")
    actor,obj=subject(case["subject"]),resource(case["resource"])
    async def wrong(*args):return replace(obj,ref=replace(obj.ref,object_id=uuid4()))
    async def validate():pass
    unit=AccessUnit(None,actor,wrong,None,validate)
    with pytest.raises(AccessFailure) as error:await unit.resource(obj.ref)
    assert error.value.status==503
    unit.closed=True
    with pytest.raises(AccessFailure):await unit.validate()
    with pytest.raises(AccessFailure):await unit.resource(obj.ref)
