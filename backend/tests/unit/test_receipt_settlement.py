from dataclasses import replace
from hashlib import sha256
import hmac
from uuid import UUID
import pytest
from enterprise_ai_platform.integrations.security.receipt_settlement import ReceiptSettlement,SettlementKey
from enterprise_ai_platform.modules.receipts.domain import PinIdentity,PinBatch,TerminalEvidence,ReceiptFailure,encoded


def uid(n):return str(UUID(int=n))


@pytest.fixture
def case():
    identity=PinIdentity(uid(1),uid(2),uid(3),'platform-dispatcher','claim.submit',uid(4),'a'*64,'draft',uid(5),2)
    batch=PinBatch(identity,uid(6),())
    evidence=TerminalEvidence(identity,'committed',batch.claim_id,1,'claim_submitted',100,'b'*64,150)
    key=SettlementKey('private',b'k'*32,90,200)
    return batch,evidence,key


def test_private_settlement_binds_full_batch_and_terminal_with_separate_protocol_domain(case):
    batch,evidence,key=case;result=ReceiptSettlement(key,clock=lambda:110).issue(batch,evidence)
    material=result['material']
    assert material==dict(schema_version=1,batch=batch.wire(),batch_digest=batch.digest,terminal=evidence.wire())
    assert result['proof']==hmac.new(b'k'*32,b'eai:receipt-settlement:v1:'+encoded(material),sha256).hexdigest()
    assert result['proof']!=hmac.new(b'k'*32,b'eai:receipt-attestation:v1:'+encoded(material),sha256).hexdigest()
    assert 'kkkk' not in repr(key)


@pytest.mark.parametrize('now',[89,150,200,float('nan'),float('inf'),True])
def test_outside_key_or_evidence_validity_cannot_mint_settlement(case,now):
    batch,evidence,key=case
    with pytest.raises(ReceiptFailure):ReceiptSettlement(key,clock=lambda:now).issue(batch,evidence)


def test_unconfigured_key_is_fail_closed(case):
    batch,evidence,_=case
    with pytest.raises(ReceiptFailure):ReceiptSettlement(clock=lambda:110).issue(batch,evidence)


def test_evidence_cannot_outlive_settlement_key(case):
    batch,evidence,key=case
    with pytest.raises(ReceiptFailure):ReceiptSettlement(replace(key,not_after=140),clock=lambda:110).issue(batch,evidence)


def test_other_identity_or_candidate_does_not_receive_settlement_mac(case):
    batch,evidence,key=case;issuer=ReceiptSettlement(key,clock=lambda:110)
    for altered in (replace(batch,claim_id=uid(20)),replace(batch,identity=replace(batch.identity,key=uid(20)))):
        with pytest.raises(ReceiptFailure):issuer.issue(altered,evidence)


def test_expiry_between_signing_and_release_is_rejected(case):
    batch,evidence,key=case;times=iter([110,150])
    with pytest.raises(ReceiptFailure):ReceiptSettlement(key,clock=lambda:next(times)).issue(batch,evidence)


@pytest.mark.parametrize('change',[dict(kid='bad key'),dict(material=b'short'),dict(not_after=90),dict(not_before=True)])
def test_invalid_private_key_configuration_is_rejected(case,change):
    with pytest.raises(ValueError):replace(case[2],**change)
