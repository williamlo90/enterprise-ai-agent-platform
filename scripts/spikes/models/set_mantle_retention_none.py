"""Operator-only Mantle Singapore retention helper. Default: read-only preview.

--apply sends at most one PUT for account-wide Mantle retention in Singapore.
No model invocation, project creation, IAM change, or automatic retry exists.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
import certifi
import httpx

REGION = 'ap-southeast-1'
MODEL = 'qwen.qwen3-235b-a22b-2507'
ORIGIN = 'https://bedrock-mantle.ap-southeast-1.api.aws'
ACCOUNT_PATH = '/v1/data_retention'
PROJECT_PATH = '/v1/organization/projects/default'
MODEL_PATH = '/v1/models/' + MODEL
READ_PATHS = {ACCOUNT_PATH, PROJECT_PATH, MODEL_PATH}
SAFE_REASONS = {'wrong_account_or_region', 'invalid_read_response', 'unexpected_model_or_project',
                'existing_policy_requires_review', 'model_none_not_supported', 'put_outcome_unknown_do_not_retry',
                'verification_failed_do_not_retry', 'claim_already_exists', 'transport_failure', 'http_rejected',
                'invalid_json', 'response_too_large', 'unsupported_request'}


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('invalid_json')
            value[key] = item
        return value
    def reject(_): raise ValueError('invalid_json')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
        if not isinstance(value, dict): raise ValueError('invalid_json')
        return value
    except Exception: raise ValueError('invalid_json') from None


class Transport:
    def __init__(self, credentials, http): self.credentials, self.http = credentials, http

    def request(self, method, path, body=None):
        if not ((method == 'GET' and path in READ_PATHS and body is None) or
                (method == 'PUT' and path == ACCOUNT_PATH and body == {'mode': 'none'})):
            raise ValueError('unsupported_request')
        payload = b'' if body is None else b'{"mode":"none"}'
        headers = {'Accept': 'application/json'}
        if body is not None: headers['Content-Type'] = 'application/json'
        request = AWSRequest(method=method, url=ORIGIN+path, data=payload, headers=headers)
        SigV4Auth(self.credentials, 'bedrock-mantle', REGION).add_auth(request)
        try:
            with self.http.stream(method, request.url, headers=dict(request.headers), content=payload) as response:
                # No redirect/retry and never include provider error bodies in output.
                if response.status_code != 200: raise ValueError('http_rejected')
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 262144: raise ValueError('response_too_large')
                return strict_json(bytes(data))
        except ValueError: raise
        except Exception: raise ValueError('transport_failure') from None


def inspect(transport, account):
    current = transport.request('GET', ACCOUNT_PATH)
    project = transport.request('GET', PROJECT_PATH)
    model = transport.request('GET', MODEL_PATH)
    if current.get('mode') not in {'inherit', 'none'}: raise ValueError('existing_policy_requires_review')
    if (project.get('id') != 'default' or project.get('status') != 'active' or
        project.get('arn') != f'arn:aws:bedrock-mantle:{REGION}:{account}:project/default'):
        raise ValueError('unexpected_model_or_project')
    if model.get('id') != MODEL or model.get('status') != 'available': raise ValueError('unexpected_model_or_project')
    retention = model.get('data_retention', {})
    if not isinstance(retention, dict) or not isinstance(project.get('data_retention'), dict): raise ValueError('invalid_read_response')
    if project['data_retention'].get('mode') != 'inherit': raise ValueError('existing_policy_requires_review')
    modes = retention.get('allowed_modes')
    if not isinstance(modes, list) or not all(isinstance(v, str) for v in modes): raise ValueError('invalid_read_response')
    if 'none' not in modes: raise ValueError('model_none_not_supported')
    mode, source = retention.get('mode'), retention.get('source')
    if mode not in {'none', 'default'} or source not in {'account', 'model_default', 'project'}:
        raise ValueError('existing_policy_requires_review')
    return {'account_mode': current['mode'], 'default_project_mode': 'inherit',
            'model_effective_mode': mode, 'model_mode_source': source, 'none_allowed': True}


def configure(setup, identity, transport, folder, *, apply=False):
    account = identity.get_caller_identity().get('Account')
    if setup.get('region') != REGION or account != setup.get('account'):
        raise ValueError('wrong_account_or_region')
    before = inspect(transport, account)
    result = {'region': REGION, 'model': MODEL, 'scope': 'Mantle_account_wide_in_Singapore',
              'context': 'implicit_default_project', 'before': before, 'applied': False,
              'inference_executed': False, 'automatic_retry': False}
    if not apply: return result
    if before['account_mode'] == 'none':
        result['verified'] = before['model_effective_mode'] == 'none'
        if not result['verified']: raise ValueError('verification_failed_do_not_retry')
        return result
    claim = folder / 'mantle-retention-apply-claim.json'
    # Claim precedes the only remote write. Any unknown outcome requires review.
    try:
        with claim.open('x', encoding='utf-8') as stream:
            json.dump({'created_at': datetime.now(timezone.utc).isoformat(), 'before': before,
                       'setup_sha256': hashlib.sha256(json.dumps(setup, sort_keys=True).encode()).hexdigest(),
                       'helper_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       'scope': result['scope'], 'desired_mode': 'none', 'state': 'claimed_before_single_put'}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError: raise ValueError('claim_already_exists') from None
    try:
        changed = transport.request('PUT', ACCOUNT_PATH, {'mode': 'none'})
        if changed.get('mode') != 'none': raise ValueError('put_outcome_unknown_do_not_retry')
    except Exception: raise ValueError('put_outcome_unknown_do_not_retry') from None
    result['applied'] = True
    result['after'] = inspect(transport, account)
    result['verified'] = result['after']['account_mode'] == result['after']['model_effective_mode'] == 'none'
    if not result['verified']: raise ValueError('verification_failed_do_not_retry')
    return result


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    folder = root / 'local/spike02/model-selection'
    try:
        setup = json.loads((root / 'local/spike02/setup.json').read_text(encoding='utf-8'))
        session = boto3.Session(region_name=REGION)
        credentials = session.get_credentials().get_frozen_credentials()
        bound = boto3.Session(aws_access_key_id=credentials.access_key, aws_secret_access_key=credentials.secret_key,
                              aws_session_token=credentials.token, region_name=REGION)
        identity = bound.client('sts', endpoint_url='https://sts.ap-southeast-1.amazonaws.com',
                                config=Config(connect_timeout=3, read_timeout=5, retries={'total_max_attempts': 1}))
        with httpx.Client(verify=certifi.where(), timeout=httpx.Timeout(10, connect=3),
                          follow_redirects=False, trust_env=False) as http:
            result = configure(setup, identity, Transport(credentials, http), folder, apply=args.apply)
        if args.apply:
            with (folder / 'mantle-retention-apply-result.json').open('x', encoding='utf-8') as stream:
                json.dump(result, stream, indent=2)
        print(json.dumps(result))
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) and str(error) in SAFE_REASONS else 'sanitized_helper_failure'
        print(json.dumps({'passed': False, 'reason': reason, 'automatic_retry': False,
                          'inference_executed': False, 'instruction': 'stop_and_return_to_Lead_do_not_repeat_apply'}))
        raise SystemExit(1) from None


if __name__ == '__main__': main()
