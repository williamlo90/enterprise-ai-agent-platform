import pytest
from reimbursement_service.infrastructure.database.migration import revisions, validate_history, MigrationError


def test_packaged_manifest_and_history():
    manifest=revisions()
    assert [(r.version,r.name) for r in manifest]==[(1,'0001_foundation.sql'),(2,'0002_operations.sql'),(3,'0003_reference_data.sql'),(4,'0004_claims.sql'),(5,'0005_receipt_candidate.sql'),(6,'0006_workflow_workspace.sql')]
    assert 'FORCE ROW LEVEL SECURITY' in manifest[0].source
    history=[(r.version,r.name,r.checksum) for r in manifest]
    validate_history([],manifest)
    validate_history(history,manifest)
    for invalid in [[(1,history[0][1],'0'*64)],history+[(4,'unexpected.sql','0'*64)],[(2,*history[0][1:])]]:
        with pytest.raises(MigrationError): validate_history(invalid,manifest)


def test_accepted_foundation_revision_is_immutable():
    assert revisions()[0].checksum == "3b858c172539a2f821fe12e3e3a32e7dbea44bbc4e55c5302b4172d1c5a3e167"


def test_accepted_operations_revision_is_immutable():
    assert revisions()[1].checksum == "09d51ba686ddf427ec323f6c77d35f89d8d29620b0828194c3f43a4a6bbbaee4"
