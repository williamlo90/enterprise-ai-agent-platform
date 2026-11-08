"""Compare shipped source/SQL bytes and exercise a clean installed wheel."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tarfile
import zipfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dist',type=Path,required=True)
    parser.add_argument('--python',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    service=Path(__file__).resolve().parents[1]
    wheels=list(args.dist.glob('reimbursement_service-*.whl'))
    sdists=list(args.dist.glob('reimbursement_service-*.tar.gz'))
    if len(wheels)!=1 or len(sdists)!=1: raise SystemExit('Expected one service wheel and sdist.')
    hashes={}
    with zipfile.ZipFile(wheels[0]) as wheel, tarfile.open(sdists[0]) as sdist:
        members={member.name.split('/',1)[-1]:member for member in sdist.getmembers() if member.isfile()}
        for path in sorted((service/'src/reimbursement_service').rglob('*')):
            if not path.is_file() or path.suffix not in {'.py','.sql'}: continue
            source=path.read_bytes(); relative=path.relative_to(service/'src').as_posix()
            if wheel.read(relative)!=source: raise SystemExit('Wheel byte mismatch: '+relative)
            member=members.get('src/'+relative)
            if member is None or sdist.extractfile(member).read()!=source: raise SystemExit('Sdist byte mismatch: '+relative)
            hashes[relative]=sha256(source).hexdigest()
    code="""
import importlib.util,json
import reimbursement_service
from reimbursement_service.infrastructure.database.migration import revisions
from reimbursement_service.main import create_app
assert importlib.util.find_spec('enterprise_ai_platform') is None
assert len(revisions())==3
print(json.dumps({'platform_import_available':False,'migration_assets':len(revisions()),'isolated_import':True}))
"""
    process=subprocess.run([str(args.python.resolve()),'-I','-c',code],cwd=args.dist.resolve(),capture_output=True,text=True)
    if process.returncode: raise SystemExit('Isolated wheel import failed.')
    proof=json.loads(process.stdout)
    proof.update(status='PASS',matched_files=len(hashes),source_hashes=hashes,
                 wheel_sha256=sha256(wheels[0].read_bytes()).hexdigest(),sdist_sha256=sha256(sdists[0].read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    print(f'Package verification PASS: {len(hashes)} source/SQL files; isolated wheel import.')


if __name__=='__main__': main()
