import asyncio
import json
from types import SimpleNamespace
import pytest
from enterprise_ai_platform import composition
from enterprise_ai_platform.config import Settings
from enterprise_ai_platform.integrations.reimbursement.draft_sources import DraftSourceReader
from enterprise_ai_platform.modules.drafts.service import DraftService
from test_reimbursement_composition import values
from test_receipt_runtime import security


def test_drafts_default_off_and_cannot_be_enabled_without_receipt_dependencies():
    assert Settings(env='test',host='127.0.0.1',port=8000).drafts_enabled is False
    with pytest.raises(ValueError):Settings(env='test',host='127.0.0.1',port=8000,drafts_enabled=True)


@pytest.mark.parametrize('bad',['yes','TRUE',1,None])
def test_draft_flag_rejects_non_boolean_operator_values(bad):
    with pytest.raises(ValueError):Settings(env='test',host='127.0.0.1',port=8000,drafts_enabled=bad)


def test_configured_drafts_share_private_database_and_close_source_HTTP_client(monkeypatch):
    class Database:
        async def open(self):pass
        async def close(self):pass
    class Storage:
        async def close(self):pass
    db=Database();store=Storage()
    monkeypatch.setattr(composition,'RuntimeDatabase',lambda *a,**kw:db)
    monkeypatch.setattr(composition.boto3,'Session',lambda *a,**kw:SimpleNamespace(client=lambda *a,**kw:object()))
    monkeypatch.setattr(composition,'S3Files',lambda *a,**kw:store)
    settings=Settings(**{**values(),'files_enabled':True,'files_bucket':'fixture-receipts','receipts_enabled':True,'receipt_security':json.dumps(security()),'drafts_enabled':True})
    resources=composition.build_resources(settings)
    assert isinstance(resources.drafts,DraftService) and resources.drafts.repository.database is db
    assert isinstance(resources.drafts.source,DraftSourceReader) and resources.drafts.source in resources.reimbursement_clients
    async def close():
        async with resources.lifecycle():assert resources.readiness.ready
        assert resources.drafts.source.http.is_closed
    asyncio.run(close())
