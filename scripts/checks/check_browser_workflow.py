"""BD-FE05 actual browser producer over owned platform28/reference6.

Storage/parser and HTTPS ASGI transport only are fixtures. No UI/cloud/model calls.
"""
from check_workflow_configured import *
import check_workflow_configured as foundation
from enterprise_ai_platform.integrations.reimbursement.workspace import WorkspaceReader
from enterprise_ai_platform.integrations.reimbursement.trips import TripCatalogReader
import enterprise_ai_platform.integrations.reimbursement.workspace as workspace_adapter
from reimbursement_service.infrastructure.database.workspace import WorkspaceRepository
DIAGNOSTICS=[]

class ControlledSource(httpx.AsyncBaseTransport):
    def __init__(self,app):self.inner=httpx.ASGITransport(app=app);self.hook=None
    async def handle_async_request(self,request):
        response=await self.inner.handle_async_request(request)
        if request.url.path=='/service/v1/workspace':TRACE.append(dict(surface='workspace_source',status=response.status_code))
        if request.url.path=='/service/v1/workspace' and self.hook is not None:
            hook=self.hook;self.hook=None;await hook()
        return response
    async def aclose(self):await self.inner.aclose()

class Harness(foundation.Harness):
    reference_revision=6
    def passed(self,name):super().passed(name);print(json.dumps(dict(check=name,passed=True)),flush=True)
    async def bridge(self):
        self.fixture_approval_route=[dict(stage=n,approver_id=str(uuid4()),scope_ref='reimbursement') for n in (1,2)]
        def workspace(target,signer):
            self.control=ControlledSource(self.routed)
            return WorkspaceReader(target,signer,transport=self.control)
        original_read=WorkspaceRepository.read;original_projection=workspace_adapter.projection
        async def traced_read(repo,*args,**kwargs):
            try:return await original_read(repo,*args,**kwargs)
            except Exception as error:
                current=error
                for _ in range(5):
                    if current is None:break
                    DIAGNOSTICS.append(dict(kind='source',type=type(current).__name__,sqlstate=getattr(current,'sqlstate',None),frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__)]))
                    current=current.__context__
                raise
        def traced_projection(*args,**kwargs):
            try:return original_projection(*args,**kwargs)
            except Exception as error:
                DIAGNOSTICS.append(dict(kind='projection',type=type(error).__name__,frames=[dict(file=Path(f.filename).name,line=f.lineno) for f in traceback.extract_tb(error.__traceback__)]));raise
        with patch.object(WorkspaceRepository,'read',traced_read),patch.object(workspace_adapter,'projection',traced_projection),patch.object(platform_composition,'WorkspaceReader',workspace),patch.object(platform_composition,'TripCatalogReader',lambda target,signer:TripCatalogReader(target,signer,transport=httpx.ASGITransport(app=self.routed))):await super().bridge()

    async def run_flow(self,browser,resources,client,pins,service,execute,trip,item,policy,body,internal):
        tenant=self.people[0]['tenant'];owner=self.people[0];reviewers=[]
        await self.execute("UPDATE {s}.internal_caller_credentials SET allowed_actions=allowed_actions||'[\"workflow.read\",\"trips.list\"]'::jsonb WHERE credential_hash=%s",(self.credential_hash,))
        for route in body['rules']['approval_route']:
            id=UUID(route['approver_id']);member=await self.member(tenant,id,grants=(RoleGrant('employee','tenant',str(tenant)),RoleGrant('approver','process','reimbursement')))
            token,csrf=await self.session(tenant,id);self.people.append(dict(tenant=tenant,person=id,member=member,token=token,csrf=csrf));reviewers.append(len(self.people)-1)
        async def headers(index=0):
            p=self.people[index];cookie={'cookie':'__Host-eai-session='+p['token']}
            me=await browser.get('/api/v1/me',headers={**cookie,'x-eai-csrf-bootstrap':'1','sec-fetch-site':'same-origin'});require(me.status_code==200,'workspace-bootstrap')
            p['csrf']=me.json()['csrf_token'];return {**cookie,'origin':ORIGIN,'content-type':'application/json','x-csrf-token':p['csrf'],'x-eai-expected-context':me.json()['context_binding']}
        async def get(path,index=0):return await browser.get(path,headers=await headers(index))
        require((await get('/api/v1/claims')).status_code==200,'initial-claims-empty')
        require((await get('/api/v1/approvals',reviewers[0])).json()['items']==[],'initial-inbox-empty')
        require((await get('/api/v1/approvals')).status_code==403,'employee-not-approval-role')
        trips=await get('/api/v1/trips');require(trips.status_code==200,'actual-trip-list')
        facts=await get('/api/v1/trips/'+trip);require(facts.status_code==200 and facts.json()['trip']['items'][0]['id']==item,'actual-trip-detail')
        require((await get('/api/v1/trips/'+trip,reviewers[0])).status_code in {403,404},'foreign-trip-denied')
        require((await get('/api/v1/claims?limit=1&limit=2')).status_code==400,'duplicate-query-denied')
        require((await get('/api/v1/claims?limit=21')).status_code==400,'limit-denied')
        self.passed('configured_browser_trip_catalog_detail_empty_workspaces_role_gates_and_strict_pagination_use_actual_current_delegation_and_source')
        uploaded=await self.upload(b'%PDF-1.4\n'+b'Fictional browser receipt '*2000,purpose='receipt',mime='application/pdf');require(uploaded.status_code==202,'workspace-file');await self.process()
        file=await self.scalar('SELECT to_jsonb(f) FROM {s}.files f WHERE id=%s',(uploaded.json()['id'],))
        await self.execute("UPDATE {s}.job_callers SET active=false WHERE tenant_id=%s AND capability='executor'",(tenant,))
        secret=secrets.token_urlsafe(32);jobs=JobService(PostgresJobRepository(self.database),secret)
        await self.execute("INSERT INTO {s}.job_callers(id,credential_hash,capability,tenant_id,expires_at) VALUES(%s,%s,'executor',%s,clock_timestamp()+interval '1 hour')",(uuid4(),jobs._credential_hash,tenant))
        async def flow(operation):
            picked=await self.scheduler.claim();require(picked.get('id')==operation,'workspace-worker-schedule')
            w=platform_composition.workflow_worker(self.flow_settings,self.database,secret,transport=ObservedTransport(self.routed))
            try:value=await w.process(Lease(UUID(picked['id']),UUID(picked['tenant_id']),picked['generation']))
            finally:await w.transport.close()
            require(value.get('state')=='committed','workspace-worker-commit');return value
        async def draft(kind='submission',cid=None,it=None,amount=500):
            r=await browser.post('/api/v1/drafts',json=dict(kind=kind,trip_id=trip,claim_id=cid,conversation_id=None,key=str(uuid4())),headers=await headers());require(r.status_code==200,'workspace-draft');id=r.json()['id'];path='/api/v1/drafts/'+id
            content=dict(currency='SGD',items=[dict(id=str(uuid4()),trip_item_id=it or item,amount_minor=amount,receipt_ids=[file['id']])],receipts=[dict(id=file['id'],version=file['version'])])
            require((await browser.patch(path,json=dict(expected_version=1,key=str(uuid4()),content=content),headers=await headers())).status_code==200,'workspace-edit')
            preview=await browser.post(path+'/preview',json=dict(expected_version=2),headers=await headers());require(preview.status_code==200,'workspace-preview')
            rules=await get(path+'/rules?material_hash='+preview.json()['material_hash']);require(rules.status_code==200 and rules.json()['rules']==body['rules'] and rules.json()['draft_version']==2,'hash-bound-rules-'+str(rules.status_code))
            require((await get(path+'/rules?material_hash='+'0'*64)).status_code==409,'wrong-hash-rules-denied')
            require((await get(path+'/rules?material_hash='+preview.json()['material_hash'],reviewers[0])).status_code==409,'foreign-rules-denied')
            return id,preview.json()
        async def confirm(id,preview):
            r=await browser.post('/api/v1/drafts/'+id+'/confirm',json=dict(expected_version=preview['draft_version'],material_hash=preview['material_hash'],key=str(uuid4())),headers=await headers());require(r.status_code==202,'workspace-confirm');return r.json()['operation_id']
        async def decision(cid,version,revision,stage,choice,index):
            r=await browser.post('/api/v1/claims/'+cid+'/decisions',json=dict(expected_version=version,revision=revision,stage=stage,decision=choice,reason='  Fictional\x01 specific information request  ',key=str(uuid4())),headers=await headers(index));require(r.status_code==202,'workspace-decision');return await flow(r.json()['operation_id'])
        d,preview=await draft();d2,_=await draft()
        rows=await get('/api/v1/drafts?limit=1');require(rows.status_code==200 and len(rows.json()['items'])==1 and rows.json()['next_after'] is not None,'owner-draft-pagination')
        following=await get('/api/v1/drafts?limit=1&after='+rows.json()['next_after']);require(following.status_code==200 and following.json()['items'][0]['id']!=rows.json()['items'][0]['id'],'owner-draft-page-two')
        require((await get('/api/v1/drafts',reviewers[0])).json()['items']==[],'foreign-drafts-empty')
        op=await confirm(d,preview)
        delivery=(await get('/api/v1/actions')).json()['items'];require(len(delivery)==1 and delivery[0]['operation_id']==op and delivery[0]['state']=='queued' and delivery[0]['result_ref'] is None,'queued-discovery-not-business-success')
        require((await get('/api/v1/claims')).json()['items']==[],'202-does-not-mean-recorded')
        result=await flow(op);cid=result['result']['ref']
        own=await get('/api/v1/claims');require(own.status_code==200 and own.json()['items'][0]['id']==cid and own.json()['items'][0]['needs_action'] is False,'actual-owner-status')
        details=await get('/api/v1/claims/'+cid);require(details.status_code==200 and details.json()['claim']['financial']['total_minor']==500 and details.json()['claim']['rules']['rules']==body['rules'],'owner-detail-'+str(details.status_code))
        active=await get('/api/v1/approvals/'+cid,reviewers[0]);require(active.status_code==200 and active.json()['claim']['receipts'][0]['id']==file['id'],'current-reviewer-detail')
        require((await get('/api/v1/claims/'+cid,reviewers[0])).status_code==404,'reviewer-cannot-use-owner-view')
        require((await get('/api/v1/approvals/'+cid,reviewers[1])).status_code==404,'future-stage-denied')
        require((await get('/api/v1/files/'+file['id']+'/claims/'+cid+'/content',reviewers[0])).status_code==200,'current-reviewer-actual-receipt')
        self.passed('actual_manual_shared_draft_hash_bound_rule_summary_owner_pagination_queued_action_reconnect_and_current_owner_reviewer_claim_receipt_detail_are_distinct_from202')
        # A second legitimate claim supplies actual source pagination.
        it2=str(uuid4());await execute("INSERT INTO {s}.trip_expense_items VALUES(%s,%s,%s,%s,%s,'travel',1000,'SGD')",(str(tenant),it2,trip,str(owner['person']),datetime.now(ZoneInfo('Asia/Singapore')).date()))
        other,pp=await draft(it=it2);op2=await confirm(other,pp);cid2=(await flow(op2))['result']['ref']
        page=await get('/api/v1/claims?limit=1');require(page.status_code==200 and page.json()['next_after'] is not None,'actual-claim-pagination')
        require((await get('/api/v1/claims?limit=1&after='+page.json()['next_after'])).status_code==200,'actual-claim-next-page')
        page=await get('/api/v1/actions?limit=1');require(page.status_code==200 and page.json()['next_after'] is not None,'actual-action-cursor')
        require((await get('/api/v1/actions?limit=1&after='+page.json()['next_after'])).status_code==200,'actual-action-next-page')
        # Source stage moves after the first snapshot and before the final read.
        async def change_stage():await decision(cid,1,1,1,'approve',reviewers[0])
        self.control.hook=change_stage
        stale=await get('/api/v1/approvals/'+cid,reviewers[0]);require(stale.status_code==404,'stage-race-no-old-detail-'+str(stale.status_code))
        stale_cursor=await get('/api/v1/approvals?after='+cid,reviewers[0]);require(stale_cursor.status_code==409,'old-stage-cursor-not-visible')
        require((await get('/api/v1/approvals/'+cid,reviewers[1])).status_code==200,'new-current-stage-visible')
        require((await get('/api/v1/files/'+file['id']+'/claims/'+cid+'/content',reviewers[0])).status_code==404,'old-stage-receipt-denied')
        self.passed('service_pagination_and_action_cursors_are_scoped_current_relation_checks_and_source_stage_move_between_snapshots_removes_former_reviewer_detail_receipt_and_cursor_without_membership_change')
        await decision(cid,2,1,2,'request_information',reviewers[1])
        info=(await get('/api/v1/claims/'+cid)).json()['claim'];require(info['state']=='needs_information' and info['decisions'][-1]['decision']=='request_information' and info['decisions'][-1]['reason']=='  Fictional\x01 specific information request  ','needs-info-reason')
        tasks=(await get('/api/v1/claims')).json()['items'];require(next(x for x in tasks if x['id']==cid)['needs_action'] is True,'owner-needs-info-task')
        require((await get('/api/v1/approvals/'+cid,reviewers[1])).status_code==404,'needs-info-not-reviewer-task')
        infod,ip=await draft('information_update',cid,amount=550);iop=await confirm(infod,ip);await flow(iop)
        current=(await get('/api/v1/approvals/'+cid,reviewers[0])).json()['claim'];require(current['revision']==2 and current['version']==4 and current['active_stage']==1 and current['decisions']==[] and all(x['state']=='pending' for x in current['stages']),'fresh-revision-current-inbox')
        self.passed('actual_needs_information_tasks_specific_reason_owner_shared_correction_confirm_and_FLOW_commit_reset_revision_route_and_current_approver_tasks_with_no_email_push_or_paid_state')
        require((await self.mutate(self.policy_fixture['source'],'withdraw')).status_code==200,'workspace-policy-withdraw')
        require((await get('/api/v1/claims/'+cid)).status_code==200 and (await get('/api/v1/approvals/'+cid,reviewers[0])).status_code==200,'policy-hold-status-readable')
        rejected=await browser.post('/api/v1/claims/'+cid+'/decisions',json=dict(expected_version=4,revision=2,stage=1,decision='approve',reason='Fictional held policy',key=str(uuid4())),headers=await headers(reviewers[0]));require(rejected.status_code in {403,409,422,503},'held-policy-no-new-action')
        self.passed('locked_rule_and_business_status_remain_readable_under_current_policy_hold_while_existing_ACT_mutations_fail_closed')
        # Current role revocation after source read invalidates final G13 recheck.
        async def revoke_role():
            await self.execute("DELETE FROM {s}.membership_roles WHERE tenant_id=%s AND membership_id=%s AND role='approver'",(tenant,self.people[reviewers[0]]['member']))
        self.control.hook=revoke_role
        revoked=await get('/api/v1/approvals/'+cid,reviewers[0]);require(revoked.status_code in {401,403,409},'role-race-no-source-body')
        p=self.people[reviewers[0]];p['token'],p['csrf']=await self.session(tenant,p['person'])
        require((await get('/api/v1/approvals',reviewers[0])).status_code==403,'new-employee-session-no-approval-grant')
        self.passed('role_revocation_after_remote_read_discards_buffered_detail_and_fresh_employee_login_cannot_obtain_approval_catalog_authority')
        old_headers=await headers()
        async def logout():
            response=await browser.post('/api/v1/auth/logout',json={},headers={k:v for k,v in old_headers.items() if k!='x-eai-expected-context'});require(response.status_code==204,'workspace-logout')
        self.control.hook=logout
        expired=await browser.get('/api/v1/claims/'+cid,headers=old_headers);require(expired.status_code==401,'logout-race-no-detail')
        self.passed('actual_logout_between_source_read_and_final_original_session_guard_discards_claim_detail_without_returning_stale_context_or_source_payload')


def main():
    logging.disable(logging.CRITICAL);parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned fixtures.')
    folder=ROOT/'local/bdfe05';folder.mkdir(parents=True,exist_ok=True);report={'passed':False}
    try:
        files=list((ROOT/'backend/src').rglob('*.py'))+list((ROOT/'backend/src').rglob('*.sql'))+list((ROOT/'reference_service/src').rglob('*.py'))+list((ROOT/'reference_service/src').rglob('*.sql'))+list((ROOT/'scripts/checks').glob('*.py'))
        hashes={str(p.resolve().relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        settings=load_settings(ROOT/'.env',require_file=True,settings_type=DatabaseProbeSettings);base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_target=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:report=runner.run(verify(direct,base,harness_type=Harness,target_version=28,operation='bridge',direct_timeout=600))
        require(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items()),'workspace-source-stable')
        report.update(source_sha256=hashes,scope='actual configured platform28/reference6 browser API/delegation/source/ACT/FLOW; controlled storage/parser/ASGI transport only; no UI/model/cloud acceptance')
    except Exception as error:
        report.update(error_type=type(error).__name__)
        if isinstance(error,CheckFailure):report['check']=str(error)
        frames=[];current=error
        for _ in range(10):
            if current is None:break
            frames.extend(dict(file=Path(f.filename).name,function=f.name,line=f.lineno) for f in traceback.extract_tb(current.__traceback__))
            if type(getattr(current,'status',None)) is int:report['dependency_status']=current.status
            if getattr(current,'sqlstate',None):report['sqlstate']=current.sqlstate
            current=current.__context__
        report['frames']=frames;report['transport_statuses']=TRACE[-8:];report['workspace_diagnostics']=DIAGNOSTICS[-8:]
    (folder/'browser-workflow-proof.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_sha256'}));raise SystemExit(0 if report['passed'] else 1)

if __name__=='__main__':main()
