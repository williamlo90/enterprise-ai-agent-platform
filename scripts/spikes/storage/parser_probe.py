"""Explicit user-run Docker parser build/probe; no daemon startup or AWS credentials."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
RECORD=ROOT/'local/spike03/parser-image.json'
OUTPUT=ROOT/'local/spike03/parser-proof.json'
IMAGE='eai-storage-parser:spike03'


class ParserProbeRejected(ValueError):pass


def require(value,code):
    if not value:raise ParserProbeRejected(code)


def command(args,timeout=15):
    result=subprocess.run(['docker',*args],capture_output=True,timeout=timeout)
    require(result.returncode==0,'docker_operation_failed')
    return result.stdout


def fingerprint():
    return hashlib.sha256(b'\0'.join((HERE/name).read_bytes() for name in ('Dockerfile','requirements.lock','file_policy.py','parser.py'))).hexdigest()


def options(name):
    require(re.fullmatch(r'eai-parser-[a-f0-9]{32}',name) is not None,'container_name')
    return ['run','-i','--name',name,'--network','none','--memory','512m','--memory-swap','512m','--cpus','1',
            '--pids-limit','32','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--user','65534:65534','--tmpfs','/tmp:rw,noexec,nosuid,size=33554432',
            '--ulimit','nofile=32:32','--ulimit','fsize=8388608:8388608','--ulimit','cpu=5:5',
            '--stop-timeout','1','--ipc','none']


def build():
    require(command(['info','--format','{{.OSType}}']).strip()==b'linux','linux_docker_required')
    # Dedicated allowlisted context; no repository, .env, .aws or host mount enters the build.
    result=subprocess.run(['docker','build','--platform','linux/amd64','--tag',IMAGE,str(HERE)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
    require(result.returncode==0,'parser_image_build_failed')
    image=json.loads(command(['image','inspect',IMAGE]))[0]
    RECORD.parent.mkdir(parents=True,exist_ok=True)
    RECORD.write_text(json.dumps(dict(image_id=image['Id'],source_sha256=fingerprint()),indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(built=True,source_sha256=fingerprint(),aws_mutated=False)))


def run_container(data,*,override=None):
    record=json.loads(RECORD.read_text(encoding='utf-8'))
    require(record['source_sha256']==fingerprint(),'parser_source_changed_rebuild_required')
    image=json.loads(command(['image','inspect',IMAGE]))[0]
    require(image['Id']==record['image_id'],'parser_image_changed')
    name='eai-parser-'+uuid4().hex
    args=['docker',*options(name)]
    if override is not None:args+=['--entrypoint','python']
    args+=[record['image_id']]
    if override is not None:args+=['-I','-c',override]
    process=None;captured=bytearray();overflow=threading.Event();deadline=False;inspected=None
    start=time.monotonic()
    try:
        process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        def writer():
            try:process.stdin.write(data);process.stdin.close()
            except (BrokenPipeError,OSError,ValueError):pass
        def reader():
            while True:
                block=process.stdout.read(1024)
                if not block:return
                if len(captured)+len(block)>4096:overflow.set()
                if not overflow.is_set():captured.extend(block)
        threads=[threading.Thread(target=writer,daemon=True),threading.Thread(target=reader,daemon=True)]
        for thread in threads:thread.start()
        while process.poll() is None:
            if overflow.is_set() or time.monotonic()-start>=12:
                deadline=True;break
            time.sleep(0.02)
        if not deadline:
            for thread in threads:thread.join(timeout=1)
            require(not any(t.is_alive() for t in threads),'parser_io_cleanup')
            inspected=json.loads(command(['inspect',name]))[0]
            host=inspected['HostConfig']
            require(host['NetworkMode']=='none' and host['ReadonlyRootfs'] and host['Memory']==512*1024**2
                    and host['MemorySwap']==512*1024**2 and host['NanoCpus']==10**9 and host['PidsLimit']==32,'container_limits')
            require(inspected['Config']['User']=='65534:65534' and not host['Privileged'] and not host.get('Binds')
                    and not host.get('Mounts') and host['CapDrop']==['ALL'],'container_privileges')
            require(any(x.startswith('no-new-privileges') for x in host['SecurityOpt']),'container_no_new_privileges')
            require(not any(v.split('=',1)[0].startswith(('AWS_','DATABASE_','OIDC_')) for v in inspected['Config']['Env']),'container_has_credentials')
            require(set(host.get('Tmpfs',{}))=={'/tmp'} and 'size=33554432' in host['Tmpfs']['/tmp'],'temp_disk_limit')
    finally:
        # Remove the whole container/cgroup, including descendants, not just the docker CLI.
        removed=subprocess.run(['docker','rm','--force',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        if process is not None:
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            for stream in (process.stdin,process.stdout):
                if stream is not None:stream.close()
        require(removed.returncode==0,'container_cleanup_failed')
    if deadline or overflow.is_set():return dict(passed=False,code='output_limit' if overflow.is_set() else 'parser_timeout',cleanup=True)
    if process.returncode!=0:return dict(passed=False,code='parser_terminated',cleanup=True,oom=bool(inspected['State'].get('OOMKilled')))
    result=json.loads(captured)
    require(isinstance(result,dict) and type(result.get('passed')) is bool,'parser_output_shape')
    return {**result,'cleanup':True}


def run():
    require(command(['info','--format','{{.OSType}}']).strip()==b'linux','linux_docker_required')
    fixtures=json.loads((HERE/'fixtures.json').read_text(encoding='utf-8'))
    checks=[]
    for case in fixtures:
        data=base64.b64decode(case['data'],validate=True)
        header=json.dumps(dict(purpose=case['purpose'],mime=case['mime'])).encode()+b'\n'
        result=run_container(header+data)
        require(result['passed']==case['expected_pass'],case['id'])
        if case.get('code'):require(result.get('code')==case['code'],case['id']+'_code')
        if result['passed']:require(result['sha256']==hashlib.sha256(data).hexdigest() and result['size']==len(data),case['id']+'_identity')
        checks.append(case['id'])
    for size,expected in ((10*1024**2,True),(10*1024**2+1,False)):
        jpeg=base64.b64decode(next(c['data'] for c in fixtures if c['id']=='receipt_jpeg'))
        data=jpeg+b'\0'*(size-len(jpeg))
        header=b'{"purpose":"receipt","mime":"image/jpeg"}\n'
        result=run_container(header+data)
        require(result['passed']==expected,'actual_size_boundary')
        if expected:require(result['size']==size and result['sha256']==hashlib.sha256(data).hexdigest(),'boundary_identity')
        else:require(result.get('code')=='size_limit','boundary_code')
        checks.append('exact_byte_limit_receipt_accepted' if expected else 'byte_limit_plus_one')
    timeout=run_container(b'',override='import subprocess,time; subprocess.Popen(["python","-c","import time;time.sleep(60)"]);time.sleep(60)')
    require(timeout.get('code')=='parser_timeout' and timeout['cleanup'],'process_tree_timeout')
    checks.append('timeout_removes_container_and_descendants')
    overflow=run_container(b'',override='import sys;sys.stdout.write("x"*1000000);sys.stdout.flush()')
    require(overflow.get('code')=='output_limit' and overflow['cleanup'],'bounded_output')
    checks.append('excess_output_fail_closed_with_cleanup')
    oom=run_container(b'',override='x=bytearray(768*1024*1024)')
    require(not oom['passed'] and oom.get('oom') is True,'memory_limit')
    checks.append('memory_cgroup_enforced')
    cpu=run_container(b'',override='while True: pass')
    require(cpu.get('code')=='parser_terminated' and not cpu.get('oom'),'cpu_limit')
    checks.append('cpu_time_limit_enforced')
    disk=run_container(b'',override='import errno,json\ntry:\n for i in range(10):\n  with open("/tmp/part"+str(i),"wb") as f:f.write(b"x"*(4*1024**2))\n print(json.dumps({"passed":True}))\nexcept OSError as e:print(json.dumps({"passed":False,"code":"disk_full","bounded":e.errno==errno.ENOSPC}))')
    require(disk.get('code')=='disk_full' and disk.get('bounded') is True,'temp_disk_bound')
    checks.append('temporary_disk_bound_enforced')
    record=json.loads(RECORD.read_text(encoding='utf-8'))
    proof=dict(passed=True,checks=checks,check_count=len(checks),image_id=record['image_id'],source_sha256=fingerprint(),
               dependencies={'pypdf':'6.21.4','pillow':'13.0.0'},network='none',memory_bytes=512*1024**2,cpu_cores=1,pids=32,
               temporary_disk_bytes=32*1024**2,wall_seconds=12,cpu_seconds=5,stdout_bytes=4096,credentials_in_container=False,cleanup='completed')
    OUTPUT.parent.mkdir(parents=True,exist_ok=True);OUTPUT.write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(passed=True,checks=len(checks),cleanup='completed')))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['build','run']);args=parser.parse_args()
    try:
        if args.command=='build':build()
        else:
            OUTPUT.parent.mkdir(parents=True,exist_ok=True);OUTPUT.write_text('{"passed":false,"status":"running"}\n',encoding='utf-8');run()
    except Exception as error:
        print(json.dumps(dict(passed=False,code=str(error) if isinstance(error,ParserProbeRejected) else 'sanitized_parser_probe_failure')),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':main()
