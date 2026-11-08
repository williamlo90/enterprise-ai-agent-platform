"""Local-only, serial auth-enabled frontend + frozen Linux Python Lambda ZIP. No AWS/DB calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[2]


def require(condition):
    if not condition:
        raise ValueError('Auth slice check rejected.')


def main():
    build = ROOT / 'local/fe-env-01' / ('build-' + uuid4().hex[:12])
    target = build / 'package'
    target.mkdir(parents=True)
    env = {**os.environ, 'EAI_FRONTEND_AUTH_ENABLED': 'true', 'RAYON_NUM_THREADS': '2', 'UV_CONCURRENT_DOWNLOADS': '2',
           'UV_CONCURRENT_INSTALLS': '1', 'UV_CONCURRENT_BUILDS': '1', 'UV_PYTHON_DOWNLOADS': 'never', 'UV_NO_PROGRESS': '1'}
    node = ROOT / ('local/frontend-runtime/node-v' + (ROOT / 'frontend/.node-version').read_text().strip() + '-win-x64/node.exe')
    subprocess.run([str(node), str(ROOT / 'frontend/scripts/run.ts'), 'build'], cwd=ROOT / 'frontend', env=env, check=True)
    requirements = build / 'requirements.txt'
    subprocess.run([sys.executable, '-m', 'uv', 'export', '--frozen', '--project', str(ROOT / 'backend'), '--no-dev', '--no-emit-project', '--output-file', str(requirements)], env=env, check=True, capture_output=True)
    subprocess.run([sys.executable, '-m', 'uv', 'pip', 'install', '--target', str(target), '--python-platform', 'x86_64-manylinux_2_28', '--python-version', '3.13', '--only-binary', ':all:', '--require-hashes', '-r', str(requirements)], env=env, check=True)
    dist = ROOT / 'frontend/dist'
    assets = {p.relative_to(dist).as_posix(): p for p in dist.rglob('*') if p.is_file() and p.name != '.gitkeep'}
    import re
    allowed = re.compile(r'assets/[A-Za-z0-9_-]+\.(?:js|css|woff2)\Z')
    require({'index.html', 'THIRD_PARTY_NOTICES.txt'} <= assets.keys())
    require(len(assets) <= 100 and all(n in {'index.html', 'THIRD_PARTY_NOTICES.txt'} or allowed.fullmatch(n) for n in assets))
    manifest = {'schema': 1, 'auth_enabled': True, 'files': {n: {'size': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for n, p in assets.items()}}
    require(all(0 < x['size'] <= 1048576 for x in manifest['files'].values()))
    artifact = build / 'frontend-auth.zip'
    with ZipFile(artifact, 'w', ZIP_DEFLATED) as archive:
        for p in target.rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
                archive.write(p, p.relative_to(target).as_posix())
        for p in (ROOT / 'backend/src').rglob('*'):
            if p.is_file() and p.suffix in {'.py', '.sql'}:
                archive.write(p, p.relative_to(ROOT / 'backend/src').as_posix())
        for name, p in assets.items():
            archive.write(p, '_frontend/' + name)
        archive.writestr('_frontend/manifest.json', json.dumps(manifest, sort_keys=True))
    with ZipFile(artifact) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)))
        require(not any(Path(n).name.startswith('.env') or n.endswith(('.pyd', '.map')) or '..' in Path(n).parts for n in names))
        unpacked = sum(i.file_size for i in archive.infolist())
        require(unpacked < 250 * 1024 * 1024)
    proof = {'passed': True, 'artifact': str(artifact.relative_to(ROOT)), 'zip_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
             'zip_bytes': artifact.stat().st_size, 'unpacked_bytes': unpacked, 'frontend': manifest, 'aws_changes': False,
             'backend_lock_sha256': hashlib.sha256((ROOT / 'backend/uv.lock').read_bytes()).hexdigest(),
             'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / 'backend/src').rglob('*') if p.suffix in {'.py', '.sql'}}}
    output = ROOT / 'local/fe-env-01/build-proof.json'
    output.write_text(json.dumps(proof, indent=2), encoding='utf8')
    print(json.dumps({k: proof[k] for k in ('passed', 'artifact', 'zip_bytes', 'unpacked_bytes', 'aws_changes')}))


if __name__ == '__main__':
    main()
