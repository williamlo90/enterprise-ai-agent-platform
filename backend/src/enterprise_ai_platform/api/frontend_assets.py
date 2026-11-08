"""Manifest-only public assets for the isolated same-origin Lambda auth slice."""
import base64
import hashlib
import json
from pathlib import Path
import re

PAGES = frozenset(('/', '/chat', '/search', '/knowledge', '/knowledge/upload', '/organization', '/tenants', '/access', '/appearance'))
SOURCE_PAGE = re.compile(r'/sources/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
MEMBER_PAGE = re.compile(r'/organization/members/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
DOCUMENT_PAGE = re.compile(r'/knowledge/documents/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
MAX_FILE = 1048576
ASSET = re.compile(r'assets/[A-Za-z0-9_-]+\.(?:js|css|woff2)\Z')
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.txt': 'text/plain; charset=utf-8', '.woff2': 'font/woff2'}


def frontend_response(event, root: Path):
    path = event['rawPath']
    if path.startswith('/api/') or path in ('/health/live', '/health/ready'):
        return None  # Never SPA fallback for API, callback, or health.
    method = event['requestContext']['http']['method']
    if method not in ('GET', 'HEAD'):
        return 405
    name = 'index.html' if path in PAGES or MEMBER_PAGE.fullmatch(path) or DOCUMENT_PAGE.fullmatch(path) or SOURCE_PAGE.fullmatch(path) else path.removeprefix('/')
    if name not in ('index.html', 'THIRD_PARTY_NOTICES.txt') and not ASSET.fullmatch(name):
        return 404
    # '%' and repeated slashes cannot match the strict asset allowlist. No path decoding here.
    manifest_bytes = (root / 'manifest.json').read_bytes()
    if len(manifest_bytes) > 65536:
        raise ValueError('Invalid asset manifest.')
    manifest = json.loads(manifest_bytes)
    if manifest.get('schema') != 1 or manifest.get('auth_enabled') is not True:
        raise ValueError('Invalid asset manifest.')
    entries = manifest['files']
    if not isinstance(entries, dict) or len(entries) > 100:
        raise ValueError('Invalid asset manifest.')
    entry = entries.get(name)
    if entry is None:
        return 404
    target = root / name
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        return 404
    size = target.stat().st_size
    if type(entry.get('size')) is not int or size != entry['size'] or not 0 < size <= MAX_FILE:
        raise ValueError('Invalid public asset.')
    content = target.read_bytes()
    if hashlib.sha256(content).hexdigest() != entry['sha256']:
        raise ValueError('Invalid public asset.')
    headers = {'content-type': MIME[target.suffix], 'cache-control': 'public, max-age=31536000, immutable' if ASSET.fullmatch(name) else 'no-store',
               'content-length': str(len(content)), 'referrer-policy': 'no-referrer', 'x-content-type-options': 'nosniff', 'content-security-policy': CSP}
    return {'statusCode': 200, 'headers': headers, 'isBase64Encoded': True,
            'body': '' if method == 'HEAD' else base64.b64encode(content).decode('ascii')}
