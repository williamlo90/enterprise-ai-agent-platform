"""Translate private dotenv files into one ignored Kubernetes Secret manifest."""
import argparse,json,os
from pathlib import Path
from dotenv import dotenv_values
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--name',choices=['platform','reference','worker'],required=True);p.add_argument('--env-file',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
values=dotenv_values(a.env_file,interpolate=False)
if not values or any(v is None or 'REPLACE_' in v for v in values.values()):raise SystemExit('Fill private configuration first; no values are printed.')
a.out.parent.mkdir(parents=True,exist_ok=True)
if a.out.exists():raise SystemExit('Refusing to overwrite an existing secret artifact.')
fd=os.open(a.out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump({'apiVersion':'v1','kind':'Secret','metadata':{'name':'eai-'+a.name,'namespace':'enterprise-ai'},'type':'Opaque','stringData':values},f,indent=2)
print('Private manifest written; apply it locally. No secret values printed.')
