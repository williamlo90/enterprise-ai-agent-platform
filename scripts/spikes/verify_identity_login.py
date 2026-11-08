"""Read-only acceptance evidence after the operator completes browser login/logout."""
import asyncio
import json
from pathlib import Path
import re
import time
import boto3
from deploy_identity_api import ROOT,identity_status,configuration_fingerprint
from check_oidc_runtime import STATE_FILE,safe_transport_logs
from enterprise_ai_platform.config import MigrationSettings,load_settings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.infrastructure.database.connection import direct_connection,parse_target

FILE=ROOT/'local/auth02/human-verification.json'
CHECK='initialization'
def require(value):
    if not value:raise ValueError('Acceptance check rejected.')

async def database(started,issuer):
    settings=load_settings(ROOT/'.env',require_file=True,settings_type=MigrationSettings)
    target=parse_target(settings.database_direct_url.get_secret_value(),direct=True)
    async with direct_connection(target) as connection:
        await connection.execute('SET TRANSACTION READ ONLY')
        rows=await (await connection.execute('''SELECT p.issuer=%s,p.active,
            s.token_hash ~ '^[a-f0-9]{64}$',s.csrf_hash ~ '^[a-f0-9]{64}$',
            s.expires_at=s.authenticated_at+interval '8 hours',s.last_seen_at=s.authenticated_at,
            s.revoked_at IS NOT NULL,s.active_tenant_id IS NULL,
            (SELECT count(*) FROM platform.memberships m WHERE m.principal_id=p.id),
            extract(epoch FROM s.created_at),extract(epoch FROM s.revoked_at),s.version
            FROM platform.sessions s JOIN platform.principals p ON p.id=s.principal_id
            WHERE s.created_at>=to_timestamp(%s) ORDER BY s.created_at''',(issuer,started))).fetchall()
        pending=(await (await connection.execute('''SELECT count(*) FROM platform.login_transactions
            WHERE created_at>=to_timestamp(%s) AND verifier IS NOT NULL''',(started,))).fetchone())[0]
    return rows,pending

def main():
    global CHECK
    safe_transport_logs()
    try:
        proof=json.loads(FILE.read_text(encoding='utf-8'));started=proof['started_at']
        require(0<time.time()-started<86400)
        CHECK='proof_invalidation'
        FILE.write_text(json.dumps({'passed':False,'started_at':started,'status':'verification_started'}),encoding='utf-8')
        state=json.loads(STATE_FILE.read_text(encoding='utf-8'));session=boto3.Session(region_name='ap-southeast-1')
        CHECK='runtime_binding';function=identity_status(session,state,enabled=True)
        canary=json.loads((ROOT/'local/auth02/cloud-enabled-canary.json').read_text(encoding='utf-8'))
        require(canary['passed'] and canary['configuration_fingerprint']==configuration_fingerprint(function))
        CHECK='database_session_metadata'
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            rows,pending=runner.run(database(started,function['Environment']['Variables']['OIDC_ISSUER']))
        require(len(rows)>=1)
        require(all(all(row[:8]) and row[8]==0 and row[11]>=3 for row in rows))
        require(pending==0)
        CHECK='correlated_runtime_logs';events=[]
        for page in session.client('logs').get_paginator('filter_log_events').paginate(logGroupName=state['outputs']['LogGroup'],startTime=started*1000):
            events.extend(page.get('events',[]))
        requests=[]
        for event in events:
            match=re.search(r'identity_request kind=([a-z_]+) stage=([a-z_]+) request_id=([A-Za-z0-9-]+) status=([0-9]+)',event['message'])
            if match:requests.append((event['timestamp'],match[1],match[2],match[3],int(match[4])))
        require(requests)
        for _,kind,stage,invocation,status in requests:
            require(all(any(part+' RequestId: '+invocation in e['message'] for e in events) for part in ('START','END','REPORT')))
        login=[r for r in requests if r[1]=='callback' and r[2]=='request' and r[4]==303]
        me=[r for r in requests if r[1]=='me' and r[4]==200]
        logout=[r for r in requests if r[1]=='logout' and r[4]==204]
        rejected=[r for r in requests if r[1]=='me' and r[4]==401]
        require(login and me and logout and rejected)
        require(any(a[0]<=b[0]<=c[0]<=d[0] for a in login for b in me for c in logout for d in rejected))
        CHECK='log_privacy_pattern_scan'
        forbidden=re.compile(r'(?:[?&]|\b)(?:code|state|id_token|access_token|refresh_token|code_verifier)=|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.|(?:authorization|cookie|set-cookie)\s*:',re.I)
        require(not any(forbidden.search(e['message']) for e in events))
        result={'passed':True,'started_at':started,'checked_at':int(time.time()),'actual_user_login':'user_confirmed_and_runtime_verified',
            'application_session_created':True,'sessions_observed':len(rows),'provider_issuer_matches':True,
            'session_and_csrf_hashes_only':True,'absolute_lifetime_8h':True,'me_did_not_extend_idle':True,
            'no_membership_or_tenant_auto_granted':True,'all_observed_sessions_revoked':True,
            'pending_verifier_count':pending,'callback_303_me_200_logout_204_then_me_401':True,
            'correlated_invocations_complete':len(requests),'auth_pattern_scan_clear':True,
            'raw_secret_value_comparison':'not_collected','configuration_fingerprint':configuration_fingerprint(function),
            'qa_review':'pending'}
        FILE.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,sort_keys=True))
    except Exception:
        print(json.dumps({'passed':False,'failed_check':CHECK,'actual_user_login':'verification_pending'}))
        raise SystemExit(1) from None

if __name__=='__main__':main()
