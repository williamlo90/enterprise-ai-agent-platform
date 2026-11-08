import base64
import hashlib
import json
import pytest
from enterprise_ai_platform.api.frontend_assets import frontend_response
from enterprise_ai_platform import lambda_api


def event(path='/', method='GET'):
    return {'version': '2.0', 'rawPath': path, 'rawQueryString': 'code=private-canary-marker',
            'requestContext': {'http': {'method': method, 'sourceIp': '198.51.100.20'}}, 'headers': {}}


@pytest.fixture
def bundle(tmp_path):
    files = {'index.html': b'<html>public</html>', 'assets/main-Ab123.js': b'console.log("public");', 'THIRD_PARTY_NOTICES.txt': b'MIT notice'}
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    (tmp_path / 'manifest.json').write_text(json.dumps({'schema': 1, 'auth_enabled': True, 'files': {n: {'size': len(c), 'sha256': hashlib.sha256(c).hexdigest()} for n, c in files.items()}}))
    return tmp_path


def test_pages_assets_mime_binary_head_and_cache(bundle):
    for path in ('/search','/sources/11111111-1111-4111-8111-111111111111'):
        assert frontend_response(event(path),bundle)['statusCode']==200
    for path in ('/', '/chat', '/knowledge', '/knowledge/upload', '/knowledge/documents/11111111-1111-4111-8111-111111111111', '/organization', '/organization/members/11111111-1111-4111-8111-111111111111', '/tenants', '/access', '/appearance'):
        result = frontend_response(event(path), bundle)
        assert base64.b64decode(result['body']) == b'<html>public</html>'
        assert result['headers']['cache-control'] == 'no-store'
        assert result['headers']['referrer-policy'] == 'no-referrer'
        assert "script-src 'self'" in result['headers']['content-security-policy']
    result = frontend_response(event('/assets/main-Ab123.js'), bundle)
    assert result['headers']['content-type'].startswith('application/javascript')
    assert 'immutable' in result['headers']['cache-control']
    head = frontend_response(event('/assets/main-Ab123.js', 'HEAD'), bundle)
    assert head['body'] == '' and head['headers']['content-length'] == result['headers']['content-length']


@pytest.mark.parametrize('path', ['/api/v1/auth/callback', '/api/v1/me', '/api/unknown', '/health/live'])
def test_api_and_health_never_become_html(bundle, path):
    assert frontend_response(event(path), bundle) is None


@pytest.mark.parametrize('path', ['/assets/../index.html', '/assets/%2e%2e/index.html', '/assets//main-Ab123.js', '/.env', '/manifest.json', '/assets/main-Ab123.js.map', '/unknown', '/knowledge/upload/extra', '/knowledge/uploads', '/knowledge//upload', '/identity-check.js', '/assets/missing.js','/organization/members/not-a-uuid','/organization/members/11111111-1111-4111-8111-111111111111/extra','/knowledge/documents/not-a-uuid','/knowledge/documents/11111111-1111-4111-8111-111111111111/extra'])
def test_only_registered_paths_are_public(bundle, path):
    assert frontend_response(event(path), bundle) == 404


@pytest.mark.parametrize('path', ['/search/extra','/sources/not-a-uuid','/sources/11111111-1111-4111-8111-111111111111/extra','/sources//11111111-1111-4111-8111-111111111111','/sources/%31'+'1'*35])
def test_source_routes_remain_strict(bundle,path):
    assert frontend_response(event(path),bundle)==404


def test_tampered_or_oversized_asset_fails_closed(bundle):
    (bundle / 'index.html').write_bytes(b'tampered')
    with pytest.raises(ValueError):
        frontend_response(event(), bundle)


def test_post_static_route_is_not_a_successful_application_mutation(bundle):
    assert frontend_response(event('/chat', 'POST'), bundle) == 405


def test_public_handler_does_not_read_secrets_database_or_raw_query(monkeypatch, caplog, bundle):
    monkeypatch.setenv('APP_FRONTEND_ENABLED', 'true')
    monkeypatch.setenv('APP_AUTH_ENABLED', 'false')
    from enterprise_ai_platform.api.frontend_assets import frontend_response as serve
    monkeypatch.setattr(lambda_api, 'frontend_response', lambda e, _root: serve(e, bundle))
    monkeypatch.setattr(lambda_api, 'load_application', lambda: pytest.fail('Static must not load secret or database'))
    result = lambda_api.handler(event(), None)
    assert result['statusCode'] == 200
    assert 'private-canary-marker' not in caplog.text and 'private-canary-marker' not in str(result)
    assert lambda_api.handler(event('/api/v1/auth/callback'), None)['statusCode'] == 404


def test_frontend_without_browser_binding_is_rejected_before_reading_secret(monkeypatch):
    monkeypatch.setenv('APP_FRONTEND_ENABLED', 'true')
    monkeypatch.setenv('APP_BROWSER_CONTEXT_ENABLED', 'false')
    monkeypatch.setattr(lambda_api.boto3, 'client', lambda *_a, **_k: pytest.fail('Readiness must fail before secrets'))
    with pytest.raises(ValueError):
        lambda_api.load_application()
