"""Built FE-ORG-01 on owned HTTPS/restricted DB, one browser, no cloud changes."""
import argparse
import asyncio
import datetime
import hashlib
import ipaddress
import json
import logging
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
from check_knowledge_workspace import Harness as WorkspaceHarness,verify
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.infrastructure.database.organization import PostgresOrganizationRepository
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.knowledge_check import write_json

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'local/fe-org-01'

def frontend_hashes():
 paths=sorted([*(ROOT/'frontend/src').rglob('*'),*(ROOT/'frontend/dist').rglob('*')])
 return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}

class Harness(WorkspaceHarness):
 async def organization_browser(self):
  before=frontend_hashes();OUT.mkdir(parents=True,exist_ok=True)
  tls=OUT/('tls-'+uuid4().hex);tls.mkdir();key_path=tls/'key.pem';cert_path=tls/'cert.pem'
  listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(32)
  origin='https://127.0.0.1:'+str(listener.getsockname()[1]);server=task=process=None
  try:
   key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Owned organization fixture')]);now=datetime.datetime.now(datetime.timezone.utc)
   cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256()))
   key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
   for index,label in ((0,'Anton Wijaya'),(3,'Administrator Uji'),(4,'Budi Santoso')):
    await self.execute('UPDATE {s}.principals SET display_name=%s WHERE id=%s',(label,self.people[index]['person']))
   identity=IdentityService(SimpleNamespace(public_origin=origin),PostgresIdentityRepository(self.database),None,None)
   organization=OrganizationService(PostgresOrganizationRepository(self.database),origin)
   app=create_app(Settings(env='test',host='127.0.0.1',port=8000).model_copy(update={'browser_context_enabled':True}),services=ApplicationServices(health=SimpleNamespace(),identity=identity,organization=organization))
   async def page():return FileResponse(ROOT/'frontend/dist/index.html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
   for path in ('/','/organization','/organization/members/{membership_id}','/chat','/knowledge','/access','/tenants','/appearance'):app.add_api_route(path,page,methods=['GET'],include_in_schema=False)
   app.mount('/assets',StaticFiles(directory=str(ROOT/'frontend/dist/assets')))
   server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=listener.getsockname()[1],ssl_keyfile=str(key_path),ssl_certfile=str(cert_path),access_log=False,log_level='critical',proxy_headers=False));task=asyncio.create_task(server.serve(sockets=[listener]))
   async with asyncio.timeout(10):
    while not server.started:
     require(not task.done(),'owned-server-start');await asyncio.sleep(.05)
   node=ROOT/('local/frontend-runtime/node-v'+(ROOT/'frontend/.node-version').read_text().strip()+'-win-x64/node.exe')
   process=subprocess.Popen([str(node),str(ROOT/'scripts/checks/frontend-organization-browser.mjs')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x08000000 if sys.platform=='win32' else 0)
   payload=dict(origin=origin,admin=self.people[3]['token'],employee=self.people[4]['token'],membership=str(self.people[0]['member']))
   stdout,_stderr=await asyncio.to_thread(process.communicate,input=json.dumps(payload).encode(),timeout=110)
   # Only bounded check names and failure stage are emitted by the owned child.
   require(len(stdout)<16384,'browser-output-bounded');(OUT/'browser-status.log').write_bytes(stdout)
   require(process.returncode==0,'browser-child-see-sanitized-status')
   proof=json.loads((OUT/'browser-proof.json').read_text());require(proof['passed'] is True,'browser-checks-pass')
   require(frontend_hashes()==before,'frontend-source-stable');self.frontend_source_sha256=before
   self.passed('built_org_browser_passes_'+str(len(proof['checks']))+'_checks_actual_restricted_api_and_responsive_layout')
  finally:
   if process is not None and process.poll() is None:process.kill();await asyncio.to_thread(process.wait,timeout=5)
   if server is not None:server.should_exit=True
   if task is not None:
    try:await asyncio.wait_for(task,5)
    except TimeoutError:task.cancel();await asyncio.gather(task,return_exceptions=True)
   listener.close();key_path.unlink(missing_ok=True);cert_path.unlink(missing_ok=True);tls.rmdir()

def main():
 logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
 if not args.live:parser.error('Explicit --live required; only owned fixtures are changed.')
 try:
  settings=load_settings(Path('.env'),require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
  with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=20,operation='organization_browser'))
  result['frontend_source_sha256']=frontend_hashes();write_json(OUT/'database-proof.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','frontend_source_sha256')}))
 except Exception as error:print('Organization browser verification failed: '+(str(error) if isinstance(error,CheckFailure) else type(error).__name__));raise SystemExit(1) from None

if __name__=='__main__':main()
