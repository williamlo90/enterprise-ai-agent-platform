"""Pinned, secret-free Docker parser. One container at a time, bounded output/tree cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import subprocess
import threading
import time
from uuid import uuid4
from enterprise_ai_platform.modules.knowledge.domain import Page,chunks,KnowledgeFailure
from enterprise_ai_platform.infrastructure.database.connection import finish_cleanup
from enterprise_ai_platform.modules.files.domain import FileFailure,MAX_FILE_BYTES


class DockerFileParser:
    def __init__(self,image_id):
        if not isinstance(image_id,str) or not re.fullmatch(r'sha256:[a-f0-9]{64}',image_id):raise ValueError('A pinned parser image is required.')
        self.image_id=image_id
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='file-parser')
        self.slots=asyncio.Semaphore(1)

    @property
    def ref(self):return self.image_id

    async def close(self):self.pool.shutdown(wait=False,cancel_futures=True)

    async def inspect(self,data,purpose,mime):
        return await self._run(data,purpose,mime,False)

    async def extract(self,data,mime):
        try:value=await self._run(data,'knowledge',mime,True)
        except FileFailure as error:raise KnowledgeFailure(422 if error.status==422 else 503) from None
        if value is False:raise KnowledgeFailure(422)
        try:
            pages=tuple(Page(row['number'],row['text']) for row in value)
            chunks(pages)
            return pages
        except Exception:raise KnowledgeFailure(422) from None

    async def _run(self,data,purpose,mime,extract):
        if not 0<len(data)<=MAX_FILE_BYTES:raise FileFailure(503)
        async with self.slots:
            future=asyncio.get_running_loop().run_in_executor(self.pool,self._inspect,data,purpose,mime,extract)
            try:return await asyncio.shield(future)
            except asyncio.CancelledError as cancelled:
                try:await finish_cleanup(asyncio.shield(future))
                except Exception:pass
                raise cancelled
            except Exception:raise FileFailure(503) from None

    def _inspect(self,data,purpose,mime,extract=False):
        name='eai-file-'+uuid4().hex
        args=['docker','run','-i','--name',name,'--network','none','--memory','512m','--memory-swap','512m','--cpus','1',
            '--pids-limit','32','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534',
            '--tmpfs','/tmp:rw,noexec,nosuid,size=33554432','--ulimit','nofile=32:32','--ulimit','fsize=8388608:8388608',
            '--ulimit','cpu=5:5','--stop-timeout','1','--ipc','none',self.image_id]
        incoming=json.dumps(dict(purpose=purpose,mime=mime,**({'operation':'extract'} if extract else {}))).encode()+b'\n'+data
        process=None;captured=bytearray();overflow=threading.Event();threads=[]
        try:
            start=time.monotonic()
            process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            def writer():
                try:process.stdin.write(incoming);process.stdin.close()
                except (BrokenPipeError,OSError,ValueError):pass
            def reader():
                while True:
                    value=process.stdout.read(1024)
                    if not value:return
                    if len(captured)+len(value)>(131072 if extract else 4096):overflow.set()
                    if not overflow.is_set():captured.extend(value)
            threads=[threading.Thread(target=writer,daemon=True),threading.Thread(target=reader,daemon=True)]
            for thread in threads:thread.start()
            while process.poll() is None:
                if overflow.is_set() or time.monotonic()-start>=12:raise FileFailure(503)
                time.sleep(0.02)
            for thread in threads:thread.join(timeout=1)
            if any(t.is_alive() for t in threads) or overflow.is_set() or process.returncode!=0:raise FileFailure(503)
            result=subprocess.run(['docker','inspect',name],capture_output=True,timeout=5)
            if result.returncode!=0:raise FileFailure(503)
            container=json.loads(result.stdout)[0];host=container['HostConfig']
            if (container['Image']!=self.image_id or host['NetworkMode']!='none' or not host['ReadonlyRootfs'] or host['Memory']!=512*1024**2
                or host['MemorySwap']!=512*1024**2 or host['NanoCpus']!=10**9 or host['PidsLimit']!=32):
                raise FileFailure(503)
            if host.get('Binds') or host.get('Mounts') or host['Privileged'] or host['CapDrop']!=['ALL'] or container['Config']['User']!='65534:65534':raise FileFailure(503)
            if not any(x.startswith('no-new-privileges') for x in host['SecurityOpt']) or set(host.get('Tmpfs',{}))!={'/tmp'}:raise FileFailure(503)
            if any(v.split('=',1)[0].startswith(('AWS_','DATABASE_','OIDC_','FILE_EXECUTOR')) for v in container['Config']['Env']):raise FileFailure(503)
            value=json.loads(captured)
            if type(value.get('passed')) is not bool:raise FileFailure(503)
            if not value['passed']:
                if set(value)!={'passed','code'}:raise FileFailure(503)
                return False
            if value.get('size')!=len(data) or value.get('sha256')!=hashlib.sha256(data).hexdigest() or value.get('detected_type')!=mime:raise FileFailure(503)
            return value.get('extracted_pages') if extract else True
        finally:
            removed=subprocess.run(['docker','rm','--force',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
            if process is not None:
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                for thread in threads:thread.join(timeout=1)
                for stream in (process.stdin,process.stdout):
                    if stream is not None:stream.close()
            if removed.returncode!=0:raise FileFailure(503)
