"""FLOW02 connected graph + shared ACT/FLOW + two-stage source proof.

Controlled native model SDK/storage/parser/HTTPS ASGI transport only; actual
owned platform27/reference5, JOB, LangGraph saver, grants and business services.
No cloud or paid model calls. Installed schema never upgraded.
"""
from check_workflow_configured import *
import check_workflow_configured as foundation
from contextlib import contextmanager
from langgraph.types import Command
from enterprise_ai_platform.integrations.checkpoints.assisted_graph import build_assisted_graph
from enterprise_ai_platform.integrations.models.workflow import NovaWorkflowPlanner
from enterprise_ai_platform.modules.assisted.domain import PREPARE

class Harness(foundation.Harness):
    def passed(self,name):
        super().passed(name);print(json.dumps(dict(check=name,passed=True)),flush=True)

    async def bridge(self):
        self.fixture_approval_route=[dict(stage=n,approver_id=str(uuid4()),scope_ref='reimbursement') for n in (1,2)]
        original=foundation.Settings
        def settings(**kwargs):return original(**{**kwargs,'conversations_enabled':True})
        with patch.object(foundation,'Settings',settings):await super().bridge()

    async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
        tenant=self.people[0]['tenant'];owner=self.people[0];reviewers=[]
        for stage in body['rules']['approval_route']:
            id=UUID(stage['approver_id']);member=await self.member(tenant,id,grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')))
            token,csrf=await self.session(tenant,id);self.people.append(dict(tenant=tenant,person=id,member=member,token=token,csrf=csrf));reviewers.append(len(self.people)-1)
        require(len(reviewers)==2,'two-actual-policy-stages')
        async def headers(index=0):
            p=self.people[index];cookie={'cookie':'__Host-eai-session='+p['token']}
            response=await browser.get('/api/v1/me',headers={**cookie,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});require(response.status_code==200,'assisted-bootstrap')
            p['csrf']=response.json()['csrf_token']
            return {**cookie,'origin':ORIGIN,'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':response.json()['context_binding']}
        uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional assisted receipt '*2000,purpose='receipt',mime='application/pdf')
        require(uploaded.status_code==202,'assisted-file');await self.process()
        file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
        await self.execute("UPDATE {s}.job_callers SET active=false WHERE tenant_id=%s AND capability='executor'",(tenant,))
        secret=secrets.token_urlsafe(32);jobs=JobService(PostgresJobRepository(self.database),secret)
        await self.execute("INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,'executor',%s,clock_timestamp()+interval '1 hour')",(uuid4(),jobs._credential_hash,tenant))
        model_calls=[]
        class SDK:
            def converse(inner,**kwargs):
                model_calls.append(kwargs)
                return dict(usage=dict(inputTokens=200,outputTokens=90,totalTokens=290),stopReason='tool_use',output=dict(message=dict(role='assistant',content=[dict(toolUse=dict(toolUseId='prepared',name=PREPARE,input=dict(currency='SGD',items=[dict(trip_item_id=item,amount_minor=500,receipt_ids=[file['id']])])))])))
        @contextmanager
        def models(observer):yield NovaWorkflowPlanner(SDK(),observer)
        async def claimed(expected_type,target=None):
            picked=await self.scheduler.claim();require(picked.get('job_type')==expected_type,'assisted-scheduler-'+str(picked.get('job_type')))
            if target is not None:
                reference=await jobs.reference(Lease(UUID(picked['id']),UUID(picked['tenant_id']),picked['generation']));require(reference['target_id']==target,'assisted-target')
            return picked
        async def graph(run,resume=False):
            picked=await claimed('agent.resume',run)
            turn=await platform_composition.build_assisted_turn(self.database,jobs,picked,source=resources.drafts.source,model_factory=models)
            if not resume:
                # Simulated process loss after committed prepare but before any
                # graph checkpoint: rebuild the real worker with the same lease.
                await turn.prepare()
                turn=await platform_composition.build_assisted_turn(self.database,jobs,picked,source=resources.drafts.source,model_factory=models)
            result=await turn.run(build_assisted_graph,resume_input=Command(resume={'approve':True}) if resume else None)
            return result,turn
        async def resume(run):
            current=await browser.get('/api/v1/runs/'+run,headers=await headers());require(current.status_code==200,'assisted-run-read')
            r=await browser.post('/api/v1/runs/'+run+'/resume',json=dict(expected_version=current.json()['version']),headers=await headers())
            require(r.status_code==202,'assisted-resume-'+str(r.status_code))
        async def flow(operation):
            picked=await claimed('action.dispatch',operation)
            w=platform_composition.workflow_worker(self.flow_settings,self.database,secret,transport=ObservedTransport(self.routed))
            try:result=await w.process(Lease(UUID(picked['id']),UUID(picked['tenant_id']),picked['generation']))
            finally:await w.transport.close()
            require(result['state']=='committed','assisted-flow-committed-'+str(result.get('state')))
            return result
        async def start(kind='submission',claim_id=None):
            r=await browser.post('/api/v1/conversations',json=dict(language='id',key=str(uuid4())),headers=await headers());require(r.status_code==201,'assisted-conversation')
            conversation=r.json()['conversation']['id'] if 'conversation' in r.json() else r.json()['id']
            selected=dict(kind=kind,trip_id=trip,claim_id=claim_id,receipts=[dict(id=file['id'],version=file['version'])])
            values=dict(text='Fictional reimbursement 500 SGD minor units',client_message_id=str(uuid4()),selection=selected)
            path='/api/v1/conversations/'+conversation+'/workflow'
            r=await browser.post(path,json=values,headers=await headers());require(r.status_code==202,'assisted-admit-'+str(r.status_code));run=r.json()['run_id']
            replay=await browser.post(path,json=values,headers=await headers());require(replay.status_code==202 and replay.json()['run_id']==run,'assisted-admit-idempotent')
            changed=copy.deepcopy(values);changed['selection']['receipts']=[]
            require((await browser.post(path,json=changed,headers=await headers())).status_code==409,'assisted-selection-key-conflict')
            result,turn=await graph(run);require(result.get('__interrupt__'),'assisted-real-persistent-interrupt')
            handle=await browser.get('/api/v1/runs/'+run+'/workflow',headers=await headers());require(handle.status_code==200,'assisted-handle-'+str(handle.status_code));draft=handle.json()['draft_id']
            require(draft is not None,'assisted-shared-draft-created')
            require((await browser.get('/api/v1/runs/'+run+'/workflow',headers=await headers(reviewers[0]))).status_code==404,'assisted-cross-owner-handle-denied')
            job=await self.scalar('SELECT state FROM {s}.outbox_jobs WHERE id=%s',(run,));require(job=='completed','waiting-releases-job')
            return run,draft,turn
        async def confirm(draft,edit=False):
            path='/api/v1/drafts/'+draft
            current=(await browser.get(path,headers=await headers())).json();version=current['version']
            if edit:
                changed=copy.deepcopy(current['content']);changed['items'][0]['amount_minor']=550
                r=await browser.patch(path,json=dict(expected_version=version,key=str(uuid4()),content=changed),headers=await headers());require(r.status_code==200,'human-edit-shared-draft');version=r.json()['version']
            preview=await browser.post(path+'/preview',json=dict(expected_version=version),headers=await headers());require(preview.status_code==200,'assisted-current-preview')
            r=await browser.post(path+'/confirm',json=dict(expected_version=version,material_hash=preview.json()['material_hash'],key=str(uuid4())),headers=await headers());require(r.status_code==202,'assisted-human-confirm-'+str(r.status_code))
            return r.json()['operation_id']
        async def decision(cid,version,revision,stage,choice,index):
            r=await browser.post('/api/v1/claims/'+cid+'/decisions',json=dict(expected_version=version,revision=revision,stage=stage,decision=choice,reason='Fictional reviewed workflow',key=str(uuid4())),headers=await headers(index))
            require(r.status_code==202,'assisted-human-decision-'+str(r.status_code));return await flow(r.json()['operation_id'])
        run,draft,old=await start()
        require(await self.scalar('SELECT count(*) FROM {s}.actions WHERE draft_id=%s',(draft,))==0,'model-never-consents')
        checkpoint=await self.scalar("SELECT string_agg(state_payload::text,' ') FROM {s}.checkpoints WHERE run_id=%s",(run,))
        require(all(x not in checkpoint for x in (trip,item,file['id'],'Fictional reimbursement','amount_minor','material_hash')),'no-raw-service-facts-grants-or-proposal-in-checkpoint')
        self.passed('actual_conversation_JOB_native_SDK_budget_shared_draft_preview_and_DB_LangGraph_interrupt_persist_only_phase_with_typed_owner_dependency')
        await resume(run);result,_=await graph(run,True)
        require(result.get('__interrupt__') and len(model_calls)==1,'resume-is-not-consent-or-second-model-call')
        require(await self.scalar('SELECT count(*) FROM {s}.drafts WHERE conversation_id=(SELECT conversation_id FROM {s}.runs WHERE id=%s)',(run,))==1,'no-duplicate-draft')
        require(await self.scalar('SELECT current_version FROM {s}.drafts WHERE id=%s',(draft,))==2,'no-replay-version-bump')
        require(await self.scalar('SELECT count(*) FROM {s}.rag_attempts WHERE run_id=%s',(run,))==1,'one-durable-provider-attempt')
        try:await old.repository.call('status',{})
        except Exception as e:require(getattr(e,'status',None)==409,'stale-old-generation')
        else:raise CheckFailure('old-writer-accepted')
        self.passed('fresh_worker_resumes_DB_checkpoint_chat_yes_never_consents_old_writer_fenced_no_duplicate_proposal_draft_version_or_paid_attempt')
        op=await confirm(draft,True)
        # Complete real dispatch first; the agent then ACKs the durable terminal.
        committed=await flow(op);cid=committed['result']['ref']
        await resume(run);result,_=await graph(run,True)
        require(result['phase']=='acknowledged' and not result.get('__interrupt__'),'durable-UI-action-ACK')
        require(await self.scalar('SELECT count(*) FROM {s}.actions WHERE draft_id=%s',(draft,))==1,'one-human-action')
        self.passed('human_manual_edit_current_version_preview_hash_and_ACT_consent_drive_real_FLOW_dispatch_then_agent_ACK_without_second_submit')
        await decision(cid,1,1,1,'approve',reviewers[0])
        # Stage one cannot decide again at stage two despite unchanged membership.
        stale=await browser.post('/api/v1/claims/'+cid+'/decisions',json=dict(expected_version=2,revision=1,stage=2,decision='approve',reason='Fictional stale reviewer',key=str(uuid4())),headers=await headers(reviewers[0]))
        require(stale.status_code in {403,404,409},'former-stage-reviewer-denied')
        await decision(cid,2,1,2,'request_information',reviewers[1])
        rows=await (await execute('SELECT state,version,current_revision_no,active_stage_no FROM {s}.claims WHERE tenant_id=%s AND id=%s',(str(tenant),cid))).fetchone()
        require(rows[0]=='needs_information' and rows[1:3]==(3,1),'actual-stage-two-needs-info')
        self.passed('actual_two_stage_policy_stage_one_approval_stage_two_needs_information_and_former_reviewer_denied_without_membership_revoke')
        irun,idraft,_=await start('information_update',cid)
        iop=await confirm(idraft);await flow(iop)
        await resume(irun);result,_=await graph(irun,True);require(result['phase']=='acknowledged','information-agent-ACK')
        rows=await (await execute('SELECT state,version,current_revision_no,active_stage_no FROM {s}.claims WHERE tenant_id=%s AND id=%s',(str(tenant),cid))).fetchone()
        require(rows==('pending_approval',4,2,1),'information-resets-route-'+str(rows))
        # Fresh revision starts from stage one; earlier approvals cannot carry.
        await decision(cid,4,2,1,'approve',reviewers[0]);await decision(cid,5,2,2,'approve',reviewers[1])
        rows=await (await execute('SELECT state,version,current_revision_no FROM {s}.claims WHERE tenant_id=%s AND id=%s',(str(tenant),cid))).fetchone()
        require(rows==('approved',6,2),'actual-approved-is-not-paid')
        self.passed('actual_agent_information_shared_form_human_ACT_FLOW_commit_increments_revision_resets_all_stages_and_requires_both_fresh_human_approvals_approved_is_not_paid')
        blocked_run,blocked_draft,_=await start()
        require((await self.mutate(self.policy_fixture['source'],'withdraw')).status_code==200,'withdraw-current-policy-source')
        await resume(blocked_run)
        try:await graph(blocked_run,True)
        except Exception as error:require(getattr(error,'status',None) in {403,409,422,503},'live-policy-rejection')
        else:raise CheckFailure('withdrawn-policy-resume-accepted')
        require(await self.scalar('SELECT count(*) FROM {s}.actions WHERE draft_id=%s',(blocked_draft,))==0,'no-action-from-withdrawn-policy')
        self.passed('current_policy_source_withdrawal_during_human_pause_blocks_resume_preview_without_issuing_human_receipt_or_dispatch')
        # Current original-session authority is rechecked at worker tools, even
        # after a successful start/budget admission and without membership revoke.
        r=await browser.post('/api/v1/conversations',json=dict(language='id',key=str(uuid4())),headers=await headers());require(r.status_code==201,'revoke-conversation')
        conversation=r.json()['conversation']['id'] if 'conversation' in r.json() else r.json()['id']
        r=await browser.post('/api/v1/conversations/'+conversation+'/workflow',json=dict(text='Fictional revoke canary',client_message_id=str(uuid4()),selection=dict(kind='submission',trip_id=trip,claim_id=None,receipts=[])),headers=await headers());require(r.status_code==202,'revoke-admit');rrun=r.json()['run_id']
        picked=await claimed('agent.resume',rrun)
        turn=await platform_composition.build_assisted_turn(self.database,jobs,picked,source=resources.drafts.source,model_factory=models)
        before=len(model_calls);original_headers=await headers()
        logged_out=await browser.post('/api/v1/auth/logout',json={},headers={k:v for k,v in original_headers.items() if k!='x-eai-expected-context'});require(logged_out.status_code==204,'actual-original-session-logout')
        try:await turn.prepare()
        except Exception as error:require(getattr(error,'status',None) in {401,403,409,503},'revoked-worker-rejection')
        else:raise CheckFailure('revoked-session-worker-accepted')
        require(len(model_calls)==before,'revoked-session-no-provider')
        require((await browser.get('/api/v1/runs/'+rrun+'/workflow',headers=original_headers)).status_code==401,'revoked-browser-handle-denied')
        require(await self.scalar('SELECT draft_id IS NULL AND proposal IS NULL FROM {s}.assisted_requests WHERE run_id=%s',(rrun,)) is True,'revoked-original-session-no-draft-or-proposal')
        self.passed('actual_logout_after_graph_start_revokes_original_session_before_tool_no_provider_draft_proposal_or_browser_handle_even_without_membership_change')



def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned fixtures.')
    folder=ROOT/'local/flow02';folder.mkdir(parents=True,exist_ok=True);report={'passed':False}
    try:
        files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))+list((ROOT/'scripts/checks').glob('*.py'))
        hashes={str(p.resolve().relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=27,operation='bridge',direct_timeout=500))
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'assisted-source-stable')
        report.update(source_sha256=hashes,scope='Actual owned platform27/reference5 graph+ACT/FLOW+two-stage; synthetic native SDK and external IO fixtures; no model-quality/cloud acceptance')
    except Exception as error:
        report.update(error_type=type(error).__name__)
        if isinstance(error,CheckFailure):report['check']=str(error)
        report['transport_statuses']=TRACE[-12:];frames=[];current=error
        for _ in range(10):
            if current is None:break
            frames.extend(dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__))
            if type(getattr(current,'status',None)) is int:report['dependency_status']=current.status
            if getattr(current,'sqlstate',None):report['sqlstate']=current.sqlstate
            current=current.__context__
        report['frames']=frames
    (folder/'assisted-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)

if __name__=='__main__':main()
