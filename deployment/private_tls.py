"""Generate operator-owned private TLS artifacts; no cluster or AWS calls."""
import argparse,base64,json,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--days',type=int,default=30);a=p.parse_args()
if not 1<=a.days<=90:raise SystemExit('Use a reviewed certificate rotation interval between1 and90days.')
a.out.mkdir(parents=True,exist_ok=False);a.out.chmod(0o700);now=datetime.now(timezone.utc)
key=rsa.generate_private_key(public_exponent=65537,key_size=3072);subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Enterprise AI private service CA')])
ca=x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=a.days+1)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(x509.KeyUsage(digital_signature=True,key_encipherment=False,content_commitment=False,data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,encipher_only=None,decipher_only=None),critical=True).sign(key,hashes.SHA256())
def write(name,data):
 path=a.out/name;fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'wb') as f:f.write(data)
write('ca.key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));ca_pem=ca.public_bytes(serialization.Encoding.PEM);write('ca.crt',ca_pem)
items=[{'apiVersion':'v1','kind':'Secret','metadata':{'name':'eai-private-ca','namespace':'enterprise-ai'},'type':'Opaque','data':{'ca.crt':base64.b64encode(ca_pem).decode()}}]
for name in ('platform','reference'):
 host=name+'-api.enterprise-ai.svc.cluster.local';private=rsa.generate_private_key(public_exponent=65537,key_size=3072)
 cert=x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,host)])).issuer_name(subject).public_key(private.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=a.days)).add_extension(x509.SubjectAlternativeName([x509.DNSName(host),x509.DNSName(name+'-api')]),critical=False).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).sign(key,hashes.SHA256())
 pem=cert.public_bytes(serialization.Encoding.PEM);private_pem=private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption());write(name+'.crt',pem);write(name+'.key',private_pem)
 items.append({'apiVersion':'v1','kind':'Secret','metadata':{'name':'eai-'+name+'-tls','namespace':'enterprise-ai'},'type':'kubernetes.io/tls','data':{'tls.crt':base64.b64encode(pem).decode(),'tls.key':base64.b64encode(private_pem).decode()}})
write('tls-manifest.json',json.dumps({'apiVersion':'v1','kind':'List','items':items},indent=2).encode())
print('Private TLS artifacts written. Keep ca.key offline; apply only tls-manifest.json.')
