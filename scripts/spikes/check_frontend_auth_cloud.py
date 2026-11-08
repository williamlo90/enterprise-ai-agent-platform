"""Operator-run bounded disabled/enabled cloud canary. No real login or paid inference."""
import argparse
import hashlib
import json
import secrets
import time
from contextlib import closing

import boto3
import httpx
from check_oidc_runtime import ROOT, STATE_FILE, safe_transport_logs, simulate
from deploy_identity_api import configuration_fingerprint
from verify_oidc_cloud import collect_complete_logs

OUT = ROOT / 'local/fe-env-01'


def require(condition):
    if not condition:
        raise ValueError('Auth slice check rejected.')


def check(phase):
    safe_transport_logs()
    plan = json.loads((OUT / 'operator-plan.json').read_text(encoding='utf8'))
    state = json.loads(STATE_FILE.read_text(encoding='utf8'))
    session = boto3.Session(region_name='ap-southeast-1')
    require(session.client('sts').get_caller_identity()['Account'] == plan['account'] == state['account'])
    with closing(session.client('lambda')) as client:
        f = client.get_function_configuration(FunctionName=plan['function_arn'])
        url = client.get_function_url_config(FunctionName=plan['function_arn'])
    require(f['FunctionArn'] == plan['function_arn'] and f['CodeSha256'] == plan['new_code_sha256'])
    require(f['Role'] == plan['role_arn'] and f['Handler'] == 'enterprise_ai_platform.lambda_api.handler')
    require(f['State'] == 'Active' and f['LastUpdateStatus'] == 'Successful')
    require(f['MemorySize'] == plan['memory_mib'] and f['Timeout'] == plan['timeout_seconds'])
    require(f['Runtime'] == 'python3.13' and f['Architectures'] == ['x86_64'] and f.get('TracingConfig', {}).get('Mode') == 'PassThrough')
    require(url['AuthType'] == 'NONE' and not url.get('Cors') and url['FunctionUrl'].rstrip('/') == plan['origin'])
    v = f['Environment']['Variables']
    enabled = phase == 'enabled'
    require(v['APP_AUTH_ENABLED'] == ('true' if enabled else 'false') and v['SPIKE_ENABLED'] == 'false')
    require(v['APP_FRONTEND_ENABLED'] == v['APP_BROWSER_CONTEXT_ENABLED'] == 'true' and v['APP_AUTH_DEMO_UI'] == 'false')
    require(v['APP_AUTH_CALLBACK_LOGGING_CONTROLLED'] == 'true' and v['PUBLIC_ORIGIN'] == plan['origin'])
    require(v['OIDC_REDIRECT_URI'] == plan['origin'] + '/api/v1/auth/callback')
    require(not {'DATABASE_URL', 'DATABASE_DIRECT_URL', 'OIDC_CLIENT_SECRET', 'APP_AUTH_VERIFIER_KEY'} & v.keys())
    with closing(session.client('iam')) as iam:
        log = f"arn:aws:logs:ap-southeast-1:{plan['account']}:log-group:{state['outputs']['LogGroup']}:*"
        simulate(iam, f['Role'], ['logs:CreateLogStream', 'logs:PutLogEvents'], log, True)
        simulate(iam, f['Role'], ['logs:CreateLogStream', 'logs:PutLogEvents'], log.removesuffix(':*') + '-forbidden:*', False)
        simulate(iam, f['Role'], ['secretsmanager:GetSecretValue'], v['SPIKE_SECRET_ARN'], True)
        simulate(iam, f['Role'], ['secretsmanager:GetSecretValue'], v['SPIKE_SECRET_ARN'] + '-forbidden', False)
        simulate(iam, f['Role'], ['iam:PassRole', 'lambda:UpdateFunctionConfiguration'], '*', False)
        simulate(iam, f['Role'], ['dynamodb:GetItem'], f"arn:aws:dynamodb:ap-southeast-1:{plan['account']}:table/{state['outputs']['TableName']}", False)
    if enabled:
        previous = json.loads((OUT / 'cloud-disabled.json').read_text())
        require(previous['passed'] and previous['configuration_fingerprint'] == configuration_fingerprint(f))
        require(0 <= time.time() - previous['checked_at'] <= 3600)
    manifest = json.loads((OUT / 'build-proof.json').read_text())['frontend']['files']
    marker = 'fe-env-canary-' + secrets.token_hex(16)
    synthetic_state = secrets.token_urlsafe(32)
    started = int(time.time() * 1000)
    invocations = []
    request_count = 0
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=55) as http:
        def get(path, status, **kwargs):
            nonlocal request_count
            request_count += 1
            require(request_count <= 12)
            r = http.get(plan['origin'] + path, **kwargs)
            require(r.status_code == status and r.headers.get('x-eai-invocation-id'))
            invocations.append(r.headers['x-eai-invocation-id'])
            return r
        for page in ('/', '/chat'):
            r = get(page, 200)
            require(r.headers['cache-control'] == 'no-store' and r.headers['referrer-policy'] == 'no-referrer')
            require("script-src 'self'" in r.headers['content-security-policy'])
            require(hashlib.sha256(r.content).hexdigest() == manifest['index.html']['sha256'])
        for name in manifest:
            if not name.startswith('assets/'):
                continue
            r = get('/' + name, 200)
            require(hashlib.sha256(r.content).hexdigest() == manifest[name]['sha256'] and 'immutable' in r.headers['cache-control'])
        get('/assets/not-present.js', 404)
        get('/.env', 404)
        r = get('/api/v1/auth/callback', 401 if enabled else 404, params={'code': marker, 'state': synthetic_state})
        require('application/json' in r.headers['content-type'] and r.headers['cache-control'] == 'no-store')
        require(r.headers['referrer-policy'] == 'no-referrer' and 'access-control-allow-origin' not in r.headers)
        get('/api/v1/me', 401 if enabled else 404, headers={'X-EAI-CSRF-Bootstrap': '1', 'Sec-Fetch-Site': 'same-origin'})
        if enabled:
            get('/health/ready', 200)
    with closing(session.client('logs')) as logs:
        messages = []
        for invocation in invocations:
            messages.extend(collect_complete_logs(logs, state['outputs']['LogGroup'], started, invocation))
    # Fixed marker never creates a valid OAuth transaction or triggers token exchange.
    require(not any(marker in m or synthetic_state in m or 'rawQueryString' in m or 'authorization_code' in m for m in messages))
    require(all(any('identity_request ' in m and ('request_id=' + invocation) in m for m in messages) for invocation in invocations))
    result = {'passed': True, 'phase': phase, 'checked_at': int(time.time()), 'code_sha256': f['CodeSha256'],
              'configuration_fingerprint': configuration_fingerprint(f), 'requests': request_count, 'complete_correlated_logs': True,
              'synthetic_callback_query_absent_from_logs': True, 'actual_user_login': 'not_tested', 'resource_changes': False}
    (OUT / ('cloud-' + phase + '.json')).write_text(json.dumps(result, indent=2), encoding='utf8')
    return {k: result[k] for k in ('passed', 'phase', 'requests', 'actual_user_login', 'resource_changes')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('disabled', 'enabled'))
    args = parser.parse_args()
    target = OUT / ('cloud-' + args.phase + '.json')
    if target.exists():
        target.unlink()  # Exact owned output; failed checks must not leave stale PASS evidence.
    try:
        print(json.dumps(check(args.phase)))
    except Exception as error:
        print(json.dumps({'passed': False, 'phase': args.phase, 'category': type(error).__name__, 'actual_user_login': 'not_tested'}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
