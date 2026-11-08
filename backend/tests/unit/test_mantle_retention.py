"""Offline tests only: no credentials, AWS clients, or network requests."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

from botocore.credentials import Credentials
import httpx
import pytest

SPEC = importlib.util.spec_from_file_location('mantle_retention_helper', Path(__file__).resolve().parents[3] / 'scripts/spikes/models/set_mantle_retention_none.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
ACCOUNT = '000000000000'
SETUP = {'account': ACCOUNT, 'region': helper.REGION}


class FakeTransport:
    def __init__(self):
        self.mode = 'inherit'
        self.calls = []
        self.fail_put = False
        self.effective_override = None

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'PUT':
            if self.fail_put: raise TimeoutError('PRIVATE PROVIDER DETAILS')
            self.mode = 'none'
            return {'mode': 'none'}
        if path == helper.ACCOUNT_PATH: return {'mode': self.mode}
        if path == helper.PROJECT_PATH:
            return {'id': 'default', 'status': 'active', 'arn': f'arn:aws:bedrock-mantle:{helper.REGION}:{ACCOUNT}:project/default', 'data_retention': {'mode': 'inherit'}}
        return {'id': helper.MODEL, 'status': 'available', 'data_retention': {
            'mode': self.effective_override or ('none' if self.mode == 'none' else 'default'),
            'source': 'account' if self.mode == 'none' else 'model_default', 'allowed_modes': ['none', 'default']}}


def identity():
    value = Mock()
    value.get_caller_identity.return_value = {'Account': ACCOUNT}
    return value


def test_preview_only_reads(tmp_path):
    transport = FakeTransport()
    result = helper.configure(SETUP, identity(), transport, tmp_path)
    assert result['before']['model_effective_mode'] == 'default'
    assert not result['applied'] and all(c[0] == 'GET' for c in transport.calls)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('setup', [SETUP | {'account': 'other'}, SETUP | {'region': 'us-east-1'}])
def test_wrong_binding_never_reaches_mantle(tmp_path, setup):
    transport = FakeTransport()
    with pytest.raises(ValueError, match='wrong_account_or_region'):
        helper.configure(setup, identity(), transport, tmp_path, apply=True)
    assert transport.calls == []


def test_single_put_claim_precedes_write_and_verified(tmp_path):
    transport = FakeTransport()
    request = transport.request
    def guarded(method, path, body=None):
        if method == 'PUT': assert (tmp_path / 'mantle-retention-apply-claim.json').exists()
        return request(method, path, body)
    transport.request = guarded
    result = helper.configure(SETUP, identity(), transport, tmp_path, apply=True)
    assert result['applied'] and result['verified']
    assert [c for c in transport.calls if c[0] == 'PUT'] == [('PUT', helper.ACCOUNT_PATH, {'mode': 'none'})]


def test_existing_claim_blocks_write(tmp_path):
    (tmp_path / 'mantle-retention-apply-claim.json').write_text('preserved', encoding='utf-8')
    transport = FakeTransport()
    with pytest.raises(ValueError, match='claim_already_exists'):
        helper.configure(SETUP, identity(), transport, tmp_path, apply=True)
    assert all(c[0] == 'GET' for c in transport.calls)
    assert (tmp_path / 'mantle-retention-apply-claim.json').read_text() == 'preserved'


def test_unknown_put_outcome_preserves_claim_no_retry(tmp_path):
    transport = FakeTransport()
    transport.fail_put = True
    with pytest.raises(ValueError, match='put_outcome_unknown_do_not_retry'):
        helper.configure(SETUP, identity(), transport, tmp_path, apply=True)
    assert len([c for c in transport.calls if c[0] == 'PUT']) == 1
    assert (tmp_path / 'mantle-retention-apply-claim.json').exists()


def test_effective_mode_must_change_not_only_account(tmp_path):
    transport = FakeTransport()
    transport.effective_override = 'default'
    with pytest.raises(ValueError, match='verification_failed_do_not_retry'):
        helper.configure(SETUP, identity(), transport, tmp_path, apply=True)
    assert len([c for c in transport.calls if c[0] == 'PUT']) == 1


def test_already_none_no_put(tmp_path):
    transport = FakeTransport()
    transport.mode = 'none'
    result = helper.configure(SETUP, identity(), transport, tmp_path, apply=True)
    assert result['verified'] and not result['applied']
    assert all(c[0] == 'GET' for c in transport.calls)


@pytest.mark.parametrize('mode', ['default', 'aws_review', 'provider_data_share', 'unknown'])
def test_existing_non_inherit_policy_requires_review(tmp_path, mode):
    transport = FakeTransport()
    transport.mode = mode
    with pytest.raises(ValueError, match='existing_policy_requires_review'):
        helper.configure(SETUP, identity(), transport, tmp_path, apply=True)
    assert all(c[0] == 'GET' for c in transport.calls)


@pytest.mark.parametrize('method,path,body', [
    ('POST', '/v1/chat/completions', {}), ('PUT', helper.ACCOUNT_PATH, {'mode': 'aws_review'}),
    ('GET', '/v1/models/other', None), ('POST', helper.PROJECT_PATH, {'data_retention': {'mode': 'none'}})])
def test_transport_allowlist_blocks_inference_and_scope_changes(method, path, body):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('network reached'))) as client:
        with pytest.raises(ValueError, match='unsupported_request'):
            helper.Transport(Credentials('test', 'test'), client).request(method, path, body)


def test_transport_signs_exact_route_and_returns_json():
    def handle(request):
        assert request.method == 'GET' and str(request.url) == helper.ORIGIN + helper.ACCOUNT_PATH
        assert '/ap-southeast-1/bedrock-mantle/aws4_request' in request.headers['authorization']
        return httpx.Response(200, json={'mode': 'inherit'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert helper.Transport(Credentials('test', 'test'), client).request('GET', helper.ACCOUNT_PATH) == {'mode': 'inherit'}


@pytest.mark.parametrize('raw', [b'{"mode":"none","mode":"default"}', b'{"mode":NaN}', b'[]'])
def test_strict_response_json(raw):
    with pytest.raises(ValueError, match='invalid_json'): helper.strict_json(raw)
