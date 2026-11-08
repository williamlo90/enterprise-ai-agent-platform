import asyncio
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID
import pytest
from enterprise_ai_platform.modules.files.service import FilesService
from enterprise_ai_platform.modules.files.domain import FileFailure
from enterprise_ai_platform.modules.receipts.domain import ClaimAccess,ReceiptRef,ReceiptFailure
from enterprise_ai_platform.integrations.reimbursement.receipt_download import ClaimDownloadAuthority
from enterprise_ai_platform.integrations.security.receipt_settlement import SettlementKey


def uid(n):return str(UUID(int=n))


@pytest.fixture
def case():
    now=[1000];data=b'%PDF-1.4 fictional receipt';digest=sha256(data).hexdigest()
    context=dict(tenant_id=uid(1),actor_id=uid(2),membership_id=uid(3),session_id=uid(4),access_revision=1,authority_expires_at=1100)
    access=ClaimAccess(uid(1),uid(2),uid(5),1,1,(ReceiptRef(uid(6),3,digest),),1040)
    reader=SimpleNamespace(access=AsyncMock(return_value=access))
    authority=ClaimDownloadAuthority(reader,SettlementKey('fixture',b'k'*32,900,1200),'a'*64,clock=lambda:now[0])
    row=dict(id=uid(6),purpose='receipt',mime='application/pdf',size_bytes=len(data),scan_state='ready',version=3,object_key='private-key',object_version='private-version',digest=digest,upload_attempts=1,replayed=False,authority_expires_at=1040)
    async def call(token,operation,payload):
        if operation=='context':return dict(context=dict(context),authority_expires_at=1100)
        return dict(row)
    repository=SimpleNamespace(claim_call=AsyncMock(side_effect=call));storage=SimpleNamespace(read=AsyncMock(return_value=data))
    service=FilesService(repository,storage,'https://fixture.test',claim_authority=authority,clock=lambda:now[0])
    return SimpleNamespace(now=now,data=data,reader=reader,access=access,context=context,row=row,repo=repository,storage=storage,service=service,authority=authority,call=call)


def test_bounded_download_rechecks_source_after_storage_and_then_actual_file_context(case):
    row,data=asyncio.run(case.service.claim_content('s'*43,uid(6),uid(5)))
    assert data==case.data and set(row)=={'id','purpose','mime','size_bytes','scan_state','version','_receipt_authority_expires_at'}
    assert row['_receipt_authority_expires_at']==1040
    assert case.reader.access.await_count==2
    assert [c.args[1] for c in case.repo.claim_call.call_args_list]==['context','reference','download']


def test_unconfigured_claim_authority_denies_without_accessing_storage(case):
    case.service.claim_authority=None
    with pytest.raises(FileFailure):asyncio.run(case.service.claim_content('s'*43,uid(6),uid(5)))
    case.storage.read.assert_not_called();case.repo.claim_call.assert_not_called()


def test_reviewer_removed_after_storage_never_releases_bytes(case):
    case.reader.access.side_effect=[case.access,ReceiptFailure(404)]
    with pytest.raises(FileFailure) as error:asyncio.run(case.service.claim_content('s'*43,uid(6),uid(5)))
    assert error.value.status==404 and [c.args[1] for c in case.repo.claim_call.call_args_list]==['context','reference']


def test_changed_current_revision_after_storage_requires_retry(case):
    case.reader.access.side_effect=[case.access,replace(case.access,claim_version=2,revision=2)]
    with pytest.raises(FileFailure) as error:asyncio.run(case.service.claim_content('s'*43,uid(6),uid(5)))
    assert error.value.status==409


def test_source_expiry_during_final_database_await_rejects_buffer(case):
    async def late(*args):
        row=await case.call(*args)
        if args[1]=='download':case.now[0]=1040
        row['authority_expires_at']=1100
        return row
    case.repo.claim_call.side_effect=late
    with pytest.raises(FileFailure) as error:asyncio.run(case.service.claim_content('s'*43,uid(6),uid(5)))
    assert error.value.status==401


def test_wrong_storage_digest_cannot_be_returned(case):
    case.storage.read.return_value=b'wrong-file'
    with pytest.raises(FileFailure) as error:asyncio.run(case.service.claim_content('s'*43,uid(6),uid(5)))
    assert error.value.status==503


def test_source_and_database_must_bind_exact_version_digest_and_actor(case):
    case.row['version']=4
    with pytest.raises(FileFailure):asyncio.run(case.service.claim_content('s'*43,uid(6),uid(5)))
    case.storage.read.assert_not_called()


def test_MAC_covers_original_session_membership_source_revision_and_refs(case):
    access,envelope=asyncio.run(case.authority.authorize(case.context,uid(5)))
    assert envelope['material']['context']=={k:v for k,v in case.context.items() if k!='authority_expires_at'}
    assert envelope['material']['access']==access.wire()
    altered={**case.context,'session_id':uid(20)}
    _,another=asyncio.run(case.authority.authorize(altered,uid(5)))
    assert another['proof']!=envelope['proof']
