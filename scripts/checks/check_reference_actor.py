"""REF-02 black-box service HTTPS -> actual platform actor verifier + DB.

Platform imports stay in this external operator test, never in service source.
Policy/file routes below are explicit controlled fixture producers only.
"""
import argparse
import asyncio
from datetime import datetime,timedelta,timezone
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import subprocess
import time
from uuid import uuid4
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.responses import JSONResponse,Response
import uvicorn
from psycopg import sql
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import ApplicationServices,database_loop_factory
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.delegation_check import Harness
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.integrations.identity.cognito_probe import safe_transport_logs
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.modules.delegation.domain import request_hash

ROOT=Path(__file__).resolve().parents[2]


def source_hashes():
    paths=[p for p in (ROOT/'reference_service').rglob('*') if p.is_file()
           and not any(part in {'.venv','__pycache__','.pytest_cache','dist','local'} for part in p.relative_to(ROOT/'reference_service').parts)
           and p.suffix in {'.py','.sql','.toml','.lock'}]
    paths.append(Path(__file__))
    return {p.relative_to(ROOT).as_posix():sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def certificate(folder):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'REF02 local fixture')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
          .not_valid_after(now+timedelta(hours=1))
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False)
          .add_extension(x509.BasicConstraints(ca=True,path_length=None),True).sign(key,hashes.SHA256()))
    certpath=folder/'fixture.pem';keypath=folder/'fixture.key'
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    return certpath,keypath


class Fixture:
    def __init__(self,actual):self.actual=actual;self.mode='ok';self.calls=0;self.credential=''
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope['path']=='/internal/v1/actors/verify':
            return await self.actual(scope,receive,send)
        self.calls+=1
        headers=dict(scope['headers'])
        if headers.get(b'authorization')!=('Bearer '+self.credential).encode():
            return await JSONResponse({},status_code=401)(scope,receive,send)
        body=bytearray()
        while True:
            message=await receive();body.extend(message.get('body',b''))
            if not message.get('more_body',False):break
        request=json.loads(body)
        result={k:v for k,v in request.items() if k not in {'data','deadline'}}
        data=request['data'];action=request['action']
        if action=='policy.eligibility':value={'eligibility_revision':1}
        elif action=='receipt.pin':value={'attestations':[{**r,'attestation_digest':'b'*64} for r in data['receipts']]}
        else:value={'state':'finalised' if data['outcome']=='committed' else 'released'}
        now=int(time.time())
        result.update(allow=True,checked_at=now,expires_at=min(now+30,request['deadline']),result=value)
        mode=self.mode
        if mode=='deny':result['allow']=False
        elif mode=='wrong_tenant':result['tenant_id']=str(uuid4())
        elif mode=='wrong_digest':result['request_digest']='f'*64
        elif mode=='expired':result['expires_at']=now
        elif mode=='wrong_result':result['result']={}
        elif mode=='wrong_receipt':result['result']['attestations'][0]['digest']='c'*64
        elif mode=='wrong_state':result['result']['state']='released'
        elif mode=='unavailable':return await JSONResponse({'secret':'never expose'},status_code=503)(scope,receive,send)
        elif mode=='redirect':return await Response(status_code=307,headers={'Location':'https://attacker.test'})(scope,receive,send)
        elif mode=='malformed':return await Response(b'{"allow":true,"allow":true}',media_type='application/json')(scope,receive,send)
        elif mode=='oversize':return await Response(b'x'*65537,media_type='application/json')(scope,receive,send)
        elif mode=='timeout':await asyncio.sleep(6)
        await JSONResponse(result)(scope,receive,send)


async def verify(direct,base,output,service_python):
    schema='db03_probe_'+uuid4().hex;database=None;server=None;server_task=None;owned=False;sock=None
    folder=output.parent/('https-'+uuid4().hex);folder.mkdir(parents=True)
    cert,key=certificate(folder);checks=[];stage='preflight'
    before=source_hashes()
    platform_before=await migrate(direct)
    extension_schema='eai_vector_probe_'+uuid4().hex;owned_extension=False
    try:
        async with direct_connection(direct) as connection:
            require(await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone() is None,'unique-namespace')
            if not await (await connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone():
                async with connection.transaction():
                    await connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
                    await connection.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
                owned_extension=True
        owned=True
        stage='owned-platform-fixture'
        await migrate(direct,upgrade=True,schema=schema)
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1])
        database=RuntimeDatabase(target,schema=schema,max_size=1);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            harness=Harness(connection,database,schema)
            settings=Settings(env='test',host='127.0.0.1',port=8000)
            services=ApplicationServices(health=None,delegation=harness.delegation)
            app=Fixture(create_internal_app(settings,services=services))
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            config=uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,proxy_headers=False,
                log_level='critical',ssl_certfile=str(cert),ssl_keyfile=str(key),lifespan='off')
            server=uvicorn.Server(config);server_task=asyncio.create_task(server.serve(sockets=[sock]))
            async with asyncio.timeout(10):
                while not server.started:
                    if server_task.done():await server_task;raise CheckFailure('server-start')
                    await asyncio.sleep(.01)
            ingress=secrets.token_urlsafe(32)

            async def child(data):
                if 'arguments' in data:data['arguments']={**data['arguments'],'deadline':int(time.time())+60}
                data.update(origin=f'https://127.0.0.1:{port}',certificate=str(cert),credential=harness.credential,ingress=ingress)
                def execute():
                    return subprocess.run([str(service_python),str(ROOT/'reference_service/scripts/actor_probe.py')],
                        input=json.dumps(data),capture_output=True,text=True,cwd=ROOT,timeout=20)
                process=await asyncio.to_thread(execute)
                require(process.returncode==0,'child-exit')
                try:return json.loads(process.stdout)
                except ValueError:raise CheckFailure('child-output') from None

            async def actor(payload,claims,*,token=None,headers=None):
                return await child(dict(payload=payload,headers=headers or [
                    ['authorization','Bearer '+ingress],['x-eai-delegation',token or harness.signing.sign(claims)],
                    ['content-type','application/json']],expected=dict(action=payload['action'],
                    object_kind=payload['object']['kind'],object_id=payload['object']['id'],object_version=payload['object']['version'])))

            stage='actor-positive-canonical-and-replay'
            payload,claims=await harness.reset();app.credential=harness.credential
            first=await actor(payload,claims);second=await actor(payload,claims)
            require(first['status']==200 and first['scope']=='actor_only' and first['local_hash']==request_hash(payload),'actual-positive')
            require(second['status']==200 and second['replayed'] is True,'actual-replay')
            checks.append(stage)
            stage='independent-canonical-v1-parity-all-six-actions'
            for action in ('trip.read','claim.read','operation.read','claim.submit','claim.information','claim.decide'):
                vector,_=await harness.reset(action)
                require((await child(dict(mode='canonical',payload=vector)))['local_hash']==request_hash(vector),'canonical-'+action)
            checks.append(stage)
            payload,claims=await harness.reset()
            stage='ingress-credential-required-and-duplicate-rejected'
            headers=[['authorization','Bearer '+secrets.token_urlsafe(32)],['x-eai-delegation',harness.signing.sign(claims)],['content-type','application/json']]
            require((await actor(payload,claims,headers=headers))['status']==401,'wrong-ingress')
            headers[0]=['authorization','Bearer '+ingress]
            require((await actor(payload,claims,headers=headers+headers[:1]))['status']==401,'duplicate-ingress')
            require((await actor(payload,claims,headers=headers+[['cookie','browser=session']]))['status']==401,'browser-cookie')
            checks.append(stage)
            for mode,status in [('wrong_signature',401),('unknown_kid',401),('HS256',401)]:
                stage='actual-signature-'+mode
                require((await actor(payload,claims,token=harness.signing.sign(claims,mode)))['status']==status,stage);checks.append(stage)
            for mode,status in [('caller_revoked',401),('role_revoked',403),('session_revoked',403),('membership_revision',403),('action_not_allowed',403)]:
                stage='actual-current-'+mode;payload,claims=await harness.reset()
                require((await actor(payload,claims))['status']==200,'before-revoke')
                await harness.database_change(mode,claims)
                result=await actor(payload,claims)
                require(result['status']==status,stage+'-'+str(result['status']));checks.append(stage)
            stage='actual-wrong-audience-and-tenant'
            payload,claims=await harness.reset();claims['aud']='wrong-service'
            require((await actor(payload,claims))['status']==401,'wrong-audience')
            payload,claims=await harness.reset();claims['tenant_id']=str(uuid4())
            require((await actor(payload,claims))['status']==403,'wrong-tenant');checks.append(stage)
            stage='actual-expired-issuer-and-originating-caller'
            for field,value in [('iss','unknown-issuer'),('caller','different-dispatcher'),('exp',int(time.time()))]:
                payload,claims=await harness.reset();claims[field]=value
                require((await actor(payload,claims))['status']==401,'claim-'+field)
            checks.append(stage)
            stage='human-receipt-unbound-holds'
            payload,claims=await harness.reset('claim.decide');harness.delegation.receipts=None
            require((await actor(payload,claims))['status']==503,'no-human-receipt-producer');checks.append(stage)

            payload,claims=await harness.reset();app.credential=harness.credential
            binding=dict(operation_id=str(uuid4()),tenant_id=str(harness.tenant_id),actor_id=str(harness.actor),deadline=int(time.time())+60)
            policy=dict(id=str(uuid4()),version=1,digest='a'*64)
            receipt=dict(id=str(uuid4()),version=1,digest='a'*64)
            claim=dict(id=str(uuid4()),version=1)
            arguments={
                'eligibility':dict(policy=policy,operation_kind='submission',trip_date='2026-11-05',claim_ref=None,**binding),
                'pin':dict(claim=claim,receipts=[receipt],**binding),
                'finalise':dict(claim=claim,outcome='committed',proof_digest='d'*64,**binding)}
            for method,values in arguments.items():
                stage='proposed-fixture-'+method
                app.mode='ok'
                require((await child(dict(mode=method,arguments=values,enabled=False)))['status']==503,'disabled-default')
                require((await child(dict(mode=method,arguments=values,enabled=True)))['status']==200,stage)
                for mode,status in [('deny',403),('wrong_tenant',503),('wrong_digest',503),('expired',503),
                                    ('wrong_result',503),('unavailable',503),('redirect',503),('malformed',503),('oversize',503)]:
                    app.mode=mode;calls=app.calls
                    require((await child(dict(mode=method,arguments=values,enabled=True)))['status']==status,stage+'-'+mode)
                    require(app.calls==calls+1,'no-http-retry')
                checks.append(stage)
            stage='eligibility-locked-claim-conditional-shape'
            app.mode='ok'
            for kind in ('information','decision'):
                bad={**arguments['eligibility'],'operation_kind':kind};calls=app.calls
                require((await child(dict(mode='eligibility',arguments=bad,enabled=True)))['status']==400,'missing-claim')
                require(app.calls==calls,'reject-before-http')
                require((await child(dict(mode='eligibility',arguments={**bad,'claim_ref':claim['id']},enabled=True)))['status']==200,'locked-claim-shape')
            checks.append(stage)
            stage='pin-version-digest-finalise-state-binding'
            app.mode='wrong_receipt'
            require((await child(dict(mode='pin',arguments=arguments['pin'],enabled=True)))['status']==503,'receipt-mismatch')
            app.mode='wrong_state'
            require((await child(dict(mode='finalise',arguments=arguments['finalise'],enabled=True)))['status']==503,'finalise-state')
            checks.append(stage)
            stage='proposed-http-timeout-no-retry'
            app.mode='timeout';calls=app.calls
            require((await child(dict(mode='eligibility',arguments=arguments['eligibility'],enabled=True)))['status']==503,'timeout')
            require(app.calls==calls+1,'timeout-no-retry');checks.append(stage)
    except Exception as exc:
        raise CheckFailure(stage+':'+(str(exc) if isinstance(exc,CheckFailure) else type(exc).__name__)) from None
    finally:
        if server is not None:server.should_exit=True
        if server_task is not None:
            async with asyncio.timeout(15):await server_task
        if sock is not None:sock.close()
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)
        if owned_extension:
            async with direct_connection(direct) as connection:
                actual=await (await connection.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
                require(actual==(extension_schema,),'extension-ownership')
                await connection.execute('DROP EXTENSION vector')
                await connection.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))
        cert.unlink(missing_ok=True);key.unlink(missing_ok=True);folder.rmdir()
    require(before==source_hashes(),'source-unchanged')
    require(await migrate(direct)==platform_before,'platform-revision-unchanged')
    return dict(status='PASS',checks=checks,source_hashes=before,cleanup=True,
        actor='actual platform internal HTTPS API + restricted PostgreSQL + service subprocess; real RSA signatures',
        policy_file='controlled proposed HTTP producers only; disabled in production; finalise digest not authority',
        business_effects=False,cloud_deployment=False,platform_revision_unchanged=True,
        platform_fixture_revision='current source migrations in disposable namespace only')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
    parser.add_argument('--output',type=Path,default=ROOT/'local/ref02/integration-proof.json')
    parser.add_argument('--service-python',type=Path,default=ROOT/'reference_service/.venv/Scripts/python.exe')
    args=parser.parse_args();safe_transport_logs();args.output.parent.mkdir(parents=True,exist_ok=True)
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
        base=parse_target(settings.database_url.get_secret_value());verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result=runner.run(verify(direct,base,args.output,args.service_python))
    except Exception as exc:
        result=dict(status='FAIL',case=str(exc) if type(exc) is CheckFailure else type(exc).__name__)
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print('REF02 verification FAIL: '+result['case']);raise SystemExit(1) from None
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(f"REF02 verification PASS: {len(result['checks'])} case groups; owned cleanup complete.")


if __name__=='__main__':main()
