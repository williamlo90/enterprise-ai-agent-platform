"""One secret-free parser pod, exec stream (not container logs), bounded cleanup."""
import hashlib,json,os,re,subprocess,threading,time
from uuid import uuid4
from enterprise_ai_platform.infrastructure.storage.parser import DockerFileParser
from enterprise_ai_platform.modules.files.domain import FileFailure

class KubernetesFileParser(DockerFileParser):
 def __init__(self,image):
  if not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}',image):raise ValueError('Pinned parser image required.')
  super().__init__(image.split('@')[1]);self.image=image
 def _inspect(self,data,purpose,mime,extract=False):
  name='parser-'+uuid4().hex;namespace='enterprise-ai-parser'
  args=['kubectl','--kubeconfig=/etc/eai/kubeconfig','-n',namespace]
  manifest={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'labels':{'app':'isolated-parser'}},'spec':{
   'automountServiceAccountToken':False,'enableServiceLinks':False,'restartPolicy':'Never','activeDeadlineSeconds':45,
   'securityContext':{'runAsNonRoot':True,'runAsUser':65534,'runAsGroup':65534,'seccompProfile':{'type':'RuntimeDefault'}},
   'containers':[{'name':'parser','image':self.image,'command':['python','-I','-c','import time; time.sleep(40)'],
    'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
    'resources':{'requests':{'cpu':'1','memory':'512Mi'},'limits':{'cpu':'1','memory':'512Mi'}},
    'volumeMounts':[{'name':'tmp','mountPath':'/tmp'}]}],'volumes':[{'name':'tmp','emptyDir':{'medium':'Memory','sizeLimit':'32Mi'}}]}}
  process=None;threads=[];buffer=bytearray();overflow=threading.Event()
  try:
   subprocess.run(args+['create','-f','-'],input=json.dumps(manifest).encode(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=8)
   subprocess.run(args+['wait','--for=condition=Ready','pod/'+name,'--timeout=25s'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=28)
   current=json.loads(subprocess.run(args+['get','pod',name,'-o','json'],capture_output=True,check=True,timeout=5).stdout)
   spec=current['spec'];container=spec['containers'][0]
   if container['image']!=self.image or spec.get('automountServiceAccountToken') is not False or container.get('env') or len(spec['containers'])!=1:raise ValueError()
   process=subprocess.Popen(args+['exec','-i',name,'--','python','-I','/app/parser.py'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
   incoming=json.dumps(dict(purpose=purpose,mime=mime,**({'operation':'extract'} if extract else {}))).encode()+b'\n'+data
   def write():
    try:process.stdin.write(incoming);process.stdin.close()
    except (OSError,ValueError):pass
   def read():
    while True:
     chunk=process.stdout.read(1024)
     if not chunk:break
     if len(buffer)+len(chunk)>(131072 if extract else 4096):overflow.set()
     if not overflow.is_set():buffer.extend(chunk)
   threads=[threading.Thread(target=write,daemon=True),threading.Thread(target=read,daemon=True)]
   for t in threads:t.start()
   start=time.monotonic()
   while process.poll() is None:
    if overflow.is_set() or time.monotonic()-start>12:raise ValueError()
    time.sleep(.05)
   for t in threads:t.join(timeout=1)
   if process.returncode or overflow.is_set() or any(t.is_alive() for t in threads):raise ValueError()
   value=json.loads(buffer)
   if value.get('passed') is False:return False
   if value.get('passed') is not True or value.get('size')!=len(data) or value.get('sha256')!=hashlib.sha256(data).hexdigest() or value.get('detected_type')!=mime:raise ValueError()
   return value.get('extracted_pages') if extract else True
  except Exception:raise FileFailure(503) from None
  finally:
   if process is not None and process.poll() is None:process.kill();process.wait(timeout=3)
   for t in threads:t.join(timeout=1)
   for stream in ((process.stdin,process.stdout) if process is not None else ()):
    if stream is not None:stream.close()
   removed=subprocess.run(args+['delete','pod',name,'--ignore-not-found','--wait=true','--timeout=10s'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=12)
   if removed.returncode:raise FileFailure(503)
