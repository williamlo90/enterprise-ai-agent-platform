"""Offline actual REF03 signer → independent platform receipt verifier check."""
from pathlib import Path
import hashlib
import json
import sys
import time
from uuid import uuid4
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'backend/src'),str(ROOT/'reference_service/src')]
from reimbursement_service.integrations.security.terminal import Signer
from reimbursement_service.modules.operations.domain import Identity,Result,Terminal
from enterprise_ai_platform.integrations.security.receipt_terminal import ReceiptTerminalVerifier,TerminalKey
from enterprise_ai_platform.modules.receipts.domain import PinIdentity,PinBatch,PinState,ReceiptFailure,finalise


def main():
    now=int(time.time());clock=lambda:now
    private=Ed25519PrivateKey.generate()
    signer=Signer(kid='owned-offline',private_bytes=private.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption()),clock=clock)
    verifier=ReceiptTerminalVerifier({'owned-offline':TerminalKey(private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw),now-1,now+120)},clock=clock)
    checks=[]
    for kind,version,result_version,code,state in (
        ('claim.submit',3,1,'claim_submitted','committed'),
        ('claim.information',7,8,'information_received','committed'),
        ('claim.submit',3,None,'sealed_no_commit','sealed_no_commit'),
    ):
        claim=str(uuid4());obj=claim if kind=='claim.information' else str(uuid4())
        identity=Identity(str(uuid4()),str(uuid4()),str(uuid4()),'platform-dispatcher',kind,str(uuid4()),'a'*64,'claim' if kind=='claim.information' else 'draft',obj,version)
        batch=PinBatch(PinIdentity(**identity.wire()),claim,())
        terminal=Terminal(identity,state,Result(claim,result_version,code) if state=='committed' else Result(None,None,code),now)
        proof=signer.issue(terminal,authority_deadline=now+30)
        evidence=verifier.verify(proof,batch);updated=finalise(PinState(batch),evidence,now=now)
        assert updated.state==('finalised' if state=='committed' else 'released')
        checks.append('actual_REF03_'+kind+'_'+state+'_wire_signature_identity_and_pin_transition')
    # An authentic signature is necessary but does not override claim semantics.
    invalid=Terminal(identity,'committed',Result(claim,99,'claim_submitted'),now)
    try:verifier.verify(signer.issue(invalid,authority_deadline=now+30),batch)
    except ReceiptFailure:pass
    else:raise RuntimeError('Wrong transition version accepted.')
    checks.append('authentic_REF03_signature_does_not_override_exact_claim_version_transition')
    files=[Path(__file__).resolve(),
        ROOT/'reference_service/src/reimbursement_service/integrations/security/terminal.py',
        ROOT/'reference_service/src/reimbursement_service/modules/operations/domain.py',
        ROOT/'backend/src/enterprise_ai_platform/integrations/security/receipt_terminal.py',
        ROOT/'backend/src/enterprise_ai_platform/modules/receipts/domain.py']
    report={'passed':True,'scope':'offline actual signer interoperability; no SQL, registry, API/download or cloud acceptance',
        'checks':checks,'external_requests':0,'private_keys':'ephemeral memory only; never persisted',
        'source_sha256':{str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    output=ROOT/'local/file02/protocol-proof.json';output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':True,'checks':len(checks),'external_requests':0}))


if __name__=='__main__':main()
