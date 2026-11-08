import asyncio
import json
from types import SimpleNamespace
import pytest
from enterprise_ai_platform import composition
from enterprise_ai_platform.config import Settings,ConfigurationError
from enterprise_ai_platform.integrations.security.receipt_configuration import receipt_security
from enterprise_ai_platform.integrations.security.delegation import RSADelegationSigner
from enterprise_ai_platform.modules.receipts.service import ReceiptPinService
from test_reimbursement_composition import values


def security():
    return dict(credential_hash='a'*64,key_id='fixture',key_hex='b'*64,not_before=100,not_after=200,
        terminal_keys={'source':dict(public_hex='c'*64,not_before=100,not_after=200,revoked=False)})


@pytest.mark.parametrize('change',[
    lambda v:v.update(key_hex='private-invalid-marker'),lambda v:v.update(not_before=True),lambda v:v.update(terminal_keys={}),
    lambda v:v['terminal_keys']['source'].update(revoked='false'),lambda v:v.update(credential_hash='wrong'),
])
def test_bad_operator_security_is_rejected_without_disclosing_values(change):
    v=security();change(v)
    with pytest.raises(ValueError) as error:receipt_security(json.dumps(v))
    assert 'private-invalid-marker' not in str(error.value)


def test_duplicate_security_fields_are_not_accepted():
    with pytest.raises(ValueError):receipt_security('{"key_id":"a","key_id":"b"}')


def test_receipts_require_complete_private_dependencies():
    with pytest.raises(ValueError):Settings(env='test',host='127.0.0.1',port=8000,receipts_enabled=True)


def test_configured_root_wires_actual_receipt_and_download_adapters_and_closes_clients(monkeypatch):
    class Database:
        async def open(self):pass
        async def close(self):pass
    class Storage:
        async def close(self):pass
    db=Database();store=Storage()
    monkeypatch.setattr(composition,'RuntimeDatabase',lambda *a,**kw:db)
    monkeypatch.setattr(composition.boto3,'Session',lambda *a,**kw:SimpleNamespace(client=lambda *a,**kw:object()))
    monkeypatch.setattr(composition,'S3Files',lambda *a,**kw:store)
    setting=Settings(**{**values(),'files_enabled':True,'files_bucket':'fixture-receipts','receipts_enabled':True,'receipt_security':json.dumps(security())})
    r=composition.build_resources(setting)
    assert isinstance(r.receipts,ReceiptPinService) and r.receipts.repository.database is db
    assert r.files.claim_authority.reader in r.reimbursement_clients and isinstance(r.files.claim_authority.reader.signer,RSADelegationSigner)
    assert r.delegation.receipts is None
    assert 'bbbbbbbb' not in repr(setting)
    async def close():
        async with r.lifecycle():assert r.readiness.ready
        assert all(c.http.is_closed for c in r.reimbursement_clients)
    asyncio.run(close())


def test_bad_security_is_rejected_before_any_database_or_provider_construction(monkeypatch):
    def forbidden(*a,**kw):pytest.fail('allocated resources with invalid security')
    monkeypatch.setattr(composition,'RuntimeDatabase',forbidden)
    setting=Settings(**{**values(),'files_enabled':True,'files_bucket':'fixture-receipts','receipts_enabled':True,'receipt_security':'private-invalid-marker'})
    with pytest.raises(ConfigurationError):composition.build_resources(setting)
