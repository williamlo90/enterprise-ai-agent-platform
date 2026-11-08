"""Single owned HTTPS/browser fixture shared by local feature checks."""
import asyncio
import datetime
import ipaddress
import json
import socket
import subprocess
import sys
from uuid import uuid4
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from enterprise_ai_platform.authorization_check import require
from check_frontend_organization import ROOT,frontend_hashes

async def run_browser(out,script,create_app,fixture,*,timeout=110):
 before=frontend_hashes();out.mkdir(parents=True,exist_ok=True)
 tls=out/('tls-'+uuid4().hex);tls.mkdir();key_path=tls/'key.pem';cert_path=tls/'cert.pem'
 listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(32)
 origin='https://127.0.0.1:'+str(listener.getsockname()[1]);server=task=process=None
 try:
  key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Owned frontend fixture')]);now=datetime.datetime.now(datetime.timezone.utc)
  cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256()))
  key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
  app=create_app(origin)
  server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=listener.getsockname()[1],ssl_keyfile=str(key_path),ssl_certfile=str(cert_path),access_log=False,log_level='critical',proxy_headers=False));task=asyncio.create_task(server.serve(sockets=[listener]))
  async with asyncio.timeout(10):
   while not server.started:
    require(not task.done(),'owned-server-start');await asyncio.sleep(.05)
  node=ROOT/('local/frontend-runtime/node-v'+(ROOT/'frontend/.node-version').read_text().strip()+'-win-x64/node.exe')
  process=subprocess.Popen([str(node),str(ROOT/'scripts/checks'/script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=0x08000000 if sys.platform=='win32' else 0)
  stdout,_stderr=await asyncio.to_thread(process.communicate,input=json.dumps(dict(origin=origin,**fixture)).encode(),timeout=timeout)
  require(len(stdout)<16384,'browser-output-bounded');(out/'browser-status.log').write_bytes(stdout)
  if process.returncode!=0:
   import re
   (out/'browser-error-kind.json').write_text(json.dumps({'returncode':process.returncode,'error_types':re.findall(rb'(?m)^(?:node:)?([A-Za-z]+Error)',_stderr)[-3:]},default=lambda v:v.decode('ascii','replace')))
  require(process.returncode==0,'browser-child-see-sanitized-status')
  proof=json.loads((out/'browser-proof.json').read_text());require(proof['passed'] is True,'browser-checks-pass')
  require(frontend_hashes()==before,'frontend-source-stable');return proof
 finally:
  if process is not None and process.poll() is None:process.kill();await asyncio.to_thread(process.wait,timeout=5)
  if server is not None:server.should_exit=True
  if task is not None:
   try:await asyncio.wait_for(task,5)
   except TimeoutError:task.cancel();await asyncio.gather(task,return_exceptions=True)
  listener.close();key_path.unlink(missing_ok=True);cert_path.unlink(missing_ok=True);tls.rmdir()
