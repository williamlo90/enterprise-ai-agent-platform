import pytest
from reimbursement_service.config import Settings,ConfigurationError


def values():return dict(database_url='fixture',catalog_enabled=True,claim_access_enabled=True,action_sources_enabled=True,workflow_enabled=True,
    platform_origin='https://platform.test',platform_credential='p'*43,dispatcher_credential='d'*43,recovery_credential='r'*43,terminal_key_id='source',terminal_private_hex='a'*64)


def test_default_off_and_valid_private_configuration_never_exposes_keys():
    assert not Settings(database_url='fixture').workflow_enabled
    config=Settings(**values());assert config.workflow_enabled
    text=repr(config);assert 'r'*43 not in text and 'a'*64 not in text


@pytest.mark.parametrize('changes',[
    dict(action_sources_enabled=False),dict(recovery_credential='d'*43),dict(recovery_credential='p'*43),dict(recovery_credential='short'),
    dict(terminal_key_id=''),dict(terminal_private_hex='invalid'),dict(recovery_credential=12),dict(workflow_enabled='true'),
])
def test_missing_shared_or_malformed_recovery_and_terminal_authority_rejected(changes):
    with pytest.raises(ConfigurationError):Settings(**{**values(),**changes})
