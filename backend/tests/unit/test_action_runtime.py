import asyncio
import json
from types import SimpleNamespace
import pytest
from enterprise_ai_platform import composition
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.infrastructure.database.actions import PostgresHumanReceiptReader
from enterprise_ai_platform.modules.actions.service import ActionService
from test_reimbursement_composition import values
from test_receipt_runtime import security


def test_actions_default_off_and_require_drafts():
    assert Settings(env='test',host='127.0.0.1',port=8000).actions_enabled is False
    with pytest.raises(ValueError):Settings(env='test',host='127.0.0.1',port=8000,actions_enabled=True)


def test_configured_action_service_replaces_receipt_fixture_with_actual_persistent_reader(monkeypatch):
    class Database:
        async def open(self):pass
        async def close(self):pass
    class Storage:
        async def close(self):pass
    db=Database();store=Storage()
    monkeypatch.setattr(composition,'RuntimeDatabase',lambda *a,**kw:db)
    monkeypatch.setattr(composition.boto3,'Session',lambda *a,**kw:SimpleNamespace(client=lambda *a,**kw:object()))
    monkeypatch.setattr(composition,'S3Files',lambda *a,**kw:store)
    settings=Settings(**{**values(),'files_enabled':True,'files_bucket':'fixture','receipts_enabled':True,'receipt_security':json.dumps(security()),'drafts_enabled':True,'actions_enabled':True})
    r=composition.build_resources(settings)
    assert isinstance(r.actions,ActionService) and r.actions.repository.database is db
    assert isinstance(r.delegation.receipts,PostgresHumanReceiptReader) and r.delegation.receipts.database is db
    assert r.actions.drafts is r.drafts.source and r.actions.decisions in r.reimbursement_clients
    async def close():
        async with r.lifecycle():assert r.readiness.ready
        assert all(c.http.is_closed for c in r.reimbursement_clients)
    asyncio.run(close())
