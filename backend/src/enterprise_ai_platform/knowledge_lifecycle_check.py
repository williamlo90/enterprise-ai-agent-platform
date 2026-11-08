"""KNOW-02 real PostgreSQL/API checks, controlled providers, no cloud or paid calls."""
import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time
from uuid import UUID,uuid4
from psycopg import sql
from psycopg.types.json import Jsonb
from enterprise_ai_platform.knowledge_check import Harness as IngestHarness,Extractor,source_hashes,write_json
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings,load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase,direct_connection,parse_target,verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate
from enterprise_ai_platform.infrastructure.database.knowledge import PostgresKnowledgeRepository
from enterprise_ai_platform.infrastructure.database.roles import provision_role,drop_probe_schema
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService
from enterprise_ai_platform.modules.knowledge.domain import KnowledgeFailure
from enterprise_ai_platform.modules.identity.service import digest

class DiagnosticRepository(PostgresKnowledgeRepository):
    async def call(self,token,operation,payload):
        try:return await super().call(token,operation,payload)
        except KnowledgeFailure as error:
            if error.status==503 and operation in ('search','search_recheck','source','source_recheck'):
                try:
                    async with self.database.transaction() as connection:
                        await connection.execute(sql.SQL('SELECT {}.knowledge_call(%s,%s,%s)').format(sql.Identifier(self.database.schema)),(token,operation,Jsonb(payload)))
                except Exception as detail:
                    code=getattr(detail,'sqlstate',None)
                    if isinstance(code,str) and len(code)==5:print(json.dumps(dict(diagnostic_operation=operation,sqlstate=code)))
            raise

class Harness(IngestHarness):
    def __init__(self,*args):
        super().__init__(*args);self.repo=DiagnosticRepository(self.database);self.knowledge=KnowledgeService(self.repo,ORIGIN)

    async def setup_lifecycle(self):
        await self.setup_files()
        tenant=self.people[0]['tenant'];person=uuid4();member=await self.member(tenant,person,grants=(RoleGrant('employee','tenant',str(tenant)),));token,csrf=await self.session(tenant,person)
        self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf))
    async def document(self,text='Fictional claims policy.',*,ready=True):
        fid=await self.ready_file(text);response=await self.create_document(fid,readers=[str(self.people[4]['person'])]);require(response.status_code==202,'admit-document');row=response.json()
        if ready:await self.process_knowledge()
        return row
    async def current(self,row):
        response=await self.status_document(row);require(response.status_code==200,'current-status');return response.json()
    async def mutate(self,row,operation,*,expected=None,key=None,index=0,readers=None,extra_headers=None):
        target=row['version_id'] if operation=='publish' else row['document_id']
        path='/api/v1/knowledge/'+('versions/'+target+'/publish' if operation=='publish' else 'documents/'+target+'/'+operation)
        values=dict(expected_version=expected if expected is not None else (await self.current(row))['document_version'],key=key or str(uuid4()))
        if operation=='acl':values['readers']=readers or []
        async with await self.client() as client:
            return await client.request('PATCH' if operation=='acl' else 'POST',path,json=values,headers=extra_headers or self.headers(index,'application/json'))
    async def search_http(self,query='claims',index=4,language='en'):
        async with await self.client() as client:
            return await client.get('/api/v1/knowledge/search',params=dict(q=query,language=language,limit=10),headers=self.headers(index))
    async def source_http(self,row,index=4):
        async with await self.client() as client:
            return await client.get('/api/v1/knowledge/sources/'+row['version_id']+'/content',headers=self.headers(index))
    async def new_version(self,row,text):
        fid=await self.ready_file(text);current=await self.current(row)
        async with await self.client() as client:
            response=await client.post('/api/v1/knowledge/documents/'+row['document_id']+'/versions',json=dict(file_id=fid,key=str(uuid4()),expected_version=current['document_version']),headers=self.headers(0,'application/json'))
        require(response.status_code==202,'new-version');value=response.json();await self.process_knowledge();return value

    async def publication_and_visibility(self):
        row=await self.document('Fictional claims policy oldword. Kebijakan klaim perjalanan.',ready=False)
        require((await self.mutate(row,'publish')).status_code==409,'publish-unready')
        require((await self.source_http(row)).status_code==404 and (await self.search_http()).json()['items']==[],'draft-private')
        self.passed('unready_draft_cannot_publish_search_or_source')
        await self.process_knowledge();key=str(uuid4());result=await self.mutate(row,'publish',key=key,expected=1)
        require(result.status_code==200,'publish-'+str(result.status_code));receipt=result.json()
        require(receipt['active_version_id']==row['version_id'] and receipt['document_version']==2 and receipt['access_revision']==2,'publish-pointer')
        require((await self.mutate(row,'publish',key=key,expected=1)).json()==receipt,'publish-replay')
        require((await self.mutate(row,'publish',key=key,expected=2)).status_code==409,'changed-publish-replay')
        require(await self.scalar('SELECT count(*) FROM {s}.knowledge_events WHERE key=%s',(UUID(key),))==1,'one-event')
        self.passed('publish_pointer_revision_audit_and_idempotency_atomic')
        search=await self.search_http();source=await self.source_http(row)
        require(search.status_code==source.status_code==200 and search.json()['search_mode']=='lexical_simple_v1' and len(search.json()['items'])==1,'reader-content')
        require(source.json()['publication_state']=='published' and source.json()['format']=='extracted_chunks' and source.json()['chunks'][0]['page']==1,'source-provenance')
        require(search.headers.get('cache-control')==source.headers.get('cache-control')=='no-store','private-cache')
        self.passed('published_reader_search_and_extracted_source_with_provenance')
        require(len((await self.search_http('klaim',language='id')).json()['items'])==1,'id-search-lexemes')
        self.passed('manual_lexical_search_accepts_id_and_en_queries_without_auto_translation')
        for index in (2,3):require((await self.source_http(row,index)).status_code==404 and (await self.search_http(index=index)).json()['items']==[],'foreign-admin-denied')
        require((await self.source_http(row,1)).status_code==200,'scoped-manager-read')
        self.passed('foreign_tenant_and_admin_denied_scoped_manager_allowed')
        replacement=await self.new_version(row,'Fictional claims policy newword.')
        require(replacement['version_no']==2 and replacement['document_version']==3,'separate-version-ordinal')
        require((await self.mutate(replacement,'publish',expected=3)).status_code==200,'replace-publish')
        require((await self.source_http(row)).json()['publication_state']=='superseded','old-source-label')
        require((await self.search_http('oldword')).json()['items']==[] and len((await self.search_http('newword')).json()['items'])==1,'only-active-search')
        require((await self.mutate(row,'publish')).status_code==409,'no-republish-superseded')
        self.passed('replace_atomic_pointer_superseded_source_and_new_only_search')
        self.active=replacement;self.original=row

    async def acl_and_withdrawal(self):
        row=self.active;before=self.embedding.calls;revision=(await self.current(row))['access_revision']
        response=await self.mutate(row,'acl',readers=[str(self.people[3]['person'])]);require(response.status_code==200 and response.json()['access_revision']==revision+1,'acl-revision')
        require((await self.source_http(row,4)).status_code==404 and (await self.search_http(index=4)).json()['items']==[],'acl-revoke-current')
        require((await self.source_http(row,3)).status_code==200 and (await self.mutate(row,'withdraw',index=3)).status_code==403,'read-not-manage')
        require(self.embedding.calls==before,'acl-no-reindex')
        self.passed('acl_revision_immediate_revoke_and_explicit_admin_read_without_management')
        count=await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE document_id=%s',(UUID(row['document_id']),));revision=(await self.current(row))['access_revision']
        invalid=await self.mutate(row,'acl',readers=[str(self.people[2]['person'])]);require(invalid.status_code==400,'foreign-grant')
        require(await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE document_id=%s',(UUID(row['document_id']),))==count and (await self.current(row))['access_revision']==revision,'foreign-grant-rollback')
        self.passed('foreign_acl_replace_rolls_back_grants_revision_and_audit')
        key=str(uuid4());version=(await self.current(row))['document_version'];withdraw=await self.mutate(row,'withdraw',expected=version,key=key)
        require(withdraw.status_code==200 and withdraw.json()['withdrawn'] and withdraw.json()['active_version_id'] is None,'withdraw-pointer')
        require((await self.mutate(row,'withdraw',expected=version,key=key)).json()==withdraw.json(),'withdraw-replay')
        for old in (self.original,row):
            for index in (0,3,4):require((await self.source_http(old,index)).status_code==404,'withdraw-all-sources')
        require((await self.search_http(index=0)).json()['items']==[] and (await self.mutate(row,'publish',expected=withdraw.json()['document_version'])).status_code==404,'no-reactivation')
        self.passed('terminal_withdraw_denies_current_history_and_republish')

    async def buffered_revocations(self):
        row=await self.document('Buffered uniqueclaims policy.');require((await self.mutate(row,'publish')).status_code==200,'buffer-publish')
        base=self.repo;h=self;hook=None
        class Interleaved:
            async def call(self,token,operation,payload):
                result=await base.call(token,operation,payload)
                if hook is not None and operation==hook[0]:await hook[1]()
                return result
        self.knowledge=KnowledgeService(Interleaved(),ORIGIN)
        async def revoke_acl():require((await h.mutate(row,'acl',readers=[])).status_code==200,'buffer-acl-mutation')
        hook=('search',revoke_acl)
        response=await self.search_http('uniqueclaims');require(response.status_code==200 and response.json()['items']==[],'buffer-search-revoked')
        self.passed('acl_revocation_between_search_fetches_discards_initial_text')
        hook=None;require((await self.mutate(row,'acl',readers=[str(self.people[4]['person'])])).status_code==200,'restore-reader')
        async def delete_source():await h.execute("UPDATE {s}.files SET deletion_state='deletion_pending' WHERE id=(SELECT file_id FROM {s}.document_versions WHERE id=%s)",(UUID(row['version_id']),))
        hook=('source',delete_source)
        require((await self.source_http(row)).status_code==404,'buffer-deleted-source')
        self.passed('file_deletion_between_source_fetches_blocks_buffered_content')
        hook=None;await self.execute("UPDATE {s}.files SET deletion_state='active' WHERE id=(SELECT file_id FROM {s}.document_versions WHERE id=%s)",(UUID(row['version_id']),))
        async def revoke_member():await h.execute('UPDATE {s}.memberships SET active=false WHERE id=%s',(self.people[4]['member'],))
        hook=('source',revoke_member)
        require((await self.source_http(row)).status_code in (401,403),'buffer-member-revoke')
        self.passed('membership_revocation_between_source_fetches_blocks_output')
        hook=None;await self.execute('UPDATE {s}.memberships SET active=true WHERE id=%s',(self.people[4]['member'],))
        async def withdraw():require((await h.mutate(row,'withdraw')).status_code==200,'buffer-withdraw')
        hook=('source',withdraw)
        require((await self.source_http(row)).status_code==404,'buffer-withdraw-blocked')
        self.passed('withdrawal_between_source_fetches_blocks_output')
        self.knowledge=KnowledgeService(base,ORIGIN)

    async def races(self):
        first=await self.document('Race claims first.');second=await self.new_version(first,'Race claims second.')
        expected=(await self.current(first))['document_version'];other_token,other_csrf=await self.session(self.people[0]['tenant'],self.people[0]['person'])
        extra={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+other_token,'x-csrf-token':other_csrf}
        a,b=await asyncio.gather(self.mutate(first,'publish',expected=expected),self.mutate(second,'publish',expected=expected,extra_headers=extra))
        require(sorted([a.status_code,b.status_code])==[200,409],'two-publish-cas')
        require(await self.scalar("SELECT count(*) FROM {s}.document_versions WHERE document_id=%s AND publication_state='published'",(UUID(first['document_id']),))==1,'one-published')
        self.passed('two_sessions_publish_same_expected_version_choose_one_winner')
        row=await self.document('Crossledger claims publish.');file_id=await self.ready_file('Crossledger claims admission.');key=str(uuid4())
        async def admission():
            async with await self.client() as client:
                return await client.post('/api/v1/knowledge/documents',json=dict(file_id=file_id,key=key,title='crossledger',readers=[]),headers=extra)
        a,b=await asyncio.gather(admission(),self.mutate(row,'publish',expected=1,key=key))
        require(sorted([a.status_code,b.status_code])==[200,409] or sorted([a.status_code,b.status_code])==[202,409],'crossledger-race')
        count=await self.scalar('SELECT (SELECT count(*) FROM {s}.knowledge_requests WHERE key=%s)+(SELECT count(*) FROM {s}.knowledge_events WHERE key=%s)',(UUID(key),UUID(key)))
        require(count==1,'one-key-ledger')
        if a.status_code==202 or b.status_code==202:await self.process_knowledge()
        self.passed('two_sessions_same_key_cross_ledger_cannot_both_commit')

    async def wait_for(self,predicate,label):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if await predicate():return
            await asyncio.sleep(.05)
        raise CheckFailure(label)

    async def controlled_key_race(self):
        row=await self.document('Barrier claims publish.');fid=await self.ready_file('Barrier claims new document.');person=self.people[0];key=str(uuid4())
        token,csrf=await self.session(person['tenant'],person['person'])
        headers={**self.headers(0,'application/json'),'cookie':'__Host-eai-session='+token,'x-csrf-token':csrf}
        async def admission():
            async with await self.client() as client:
                return await client.post('/api/v1/knowledge/documents',json=dict(file_id=fid,key=key,title='key barrier',readers=[]),headers=headers)
        lock_value=await self.scalar("SELECT hashtextextended('eai:knowledge-key:'||%s||':'||%s||':'||%s,0)",(str(person['tenant']),str(person['person']),key))
        task=None
        try:
            async with self.database.transaction() as connection:
                # First transaction performs publish but deliberately does not commit.
                payload=dict(id=row['version_id'],expected_version=1,key=key.upper(),csrf_hash=digest('csrf',person['csrf']))
                result=(await (await connection.execute(sql.SQL('SELECT {}.knowledge_call(%s,%s,%s)').format(sql.Identifier(self.schema)),(digest('session',person['token']),'publish',Jsonb(payload)))).fetchone())[0]
                require(result.get('status')=='ok','key-barrier-publish')
                task=asyncio.create_task(admission())
                async def waiting():
                    return await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted AND classid::bigint=%s AND objid::bigint=%s AND objsubid=1)",((lock_value>>32)&0xffffffff,lock_value&0xffffffff))
                await self.wait_for(waiting,'second-request-never-waited-on-shared-key')
                require(not task.done(),'key-barrier-admission-finished')
            require((await task).status_code==409,'cross-ledger-after-commit-conflict')
        finally:
            if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        require(await self.scalar('SELECT (SELECT count(*) FROM {s}.knowledge_requests WHERE key=%s)+(SELECT count(*) FROM {s}.knowledge_events WHERE key=%s)',(UUID(key),UUID(key)))==1,'key-barrier-single-outcome')
        self.passed('controlled_cross_ledger_overlap_waits_on_canonical_key_then_conflicts')

    async def controlled_recheck_race(self):
        rows=[await self.document('Locked lockclaims alpha.'),await self.document('Locked lockclaims beta.')]
        for row in rows:require((await self.mutate(row,'publish')).status_code==200,'lockcase-publish')
        first,second=sorted(rows,key=lambda row:row['document_id']);person=self.people[4]
        initial=await self.repo.call(digest('session',person['token']),'search',dict(query='lockclaims',language='en',limit=10))
        refs=[{key:hit[key] for key in ('version_id','chunk_no','index_generation','access_revision','text_digest')} for hit in initial['items']]
        require(len(refs)==2,'lockcase-two-candidates')
        reader=None;writer=None;started=asyncio.Event();pid=[]
        async def read_final():
            async with self.database.transaction() as connection:
                pid.append((await (await connection.execute('SELECT pg_backend_pid()')).fetchone())[0]);started.set()
                return (await (await connection.execute(sql.SQL('SELECT {}.knowledge_call(%s,%s,%s)').format(sql.Identifier(self.schema)),(digest('session',person['token']),'search_recheck',Jsonb(dict(items=refs))))).fetchone())[0]
        try:
            async with direct_connection(self.direct,timeout=30) as blocker:
                async with blocker.transaction():
                    await blocker.execute(sql.SQL('SELECT id FROM {}.documents WHERE id=%s FOR UPDATE').format(sql.Identifier(self.schema)),(UUID(second['document_id']),))
                    reader=asyncio.create_task(read_final());await asyncio.wait_for(started.wait(),3)
                    async def reader_waiting():
                        return await self.scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=%s AND wait_event_type='Lock')",(pid[0],))
                    await self.wait_for(reader_waiting,'recheck-did-not-lock-candidate-set')
                    writer=asyncio.create_task(self.mutate(first,'acl',readers=[]))
                    async def writer_waiting():
                        return await self.scalar('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid<>%s AND %s=ANY(pg_blocking_pids(pid)))',(pid[0],pid[0]))
                    await self.wait_for(writer_waiting,'acl-commit-not-blocked-by-final-recheck')
                    require(not reader.done() and not writer.done(),'recheck-barrier-overlap')
            result=await reader;require(result.get('status')=='ok' and len(result['items'])==2,'consistent-read-snapshot')
            require((await writer).status_code==200,'acl-after-reader-commit')
        finally:
            for task in (reader,writer):
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        after=(await self.search_http('lockclaims')).json()['items'];require(len(after)==1 and after[0]['document_id']==second['document_id'],'next-read-revoked-hit-removed')
        self.passed('final_search_recheck_locks_all_candidates_before_buffer_and_blocks_acl_commit')
        # Source's final guard must hold its locks through transaction commit too.
        source=await self.repo.call(digest('session',person['token']),'source',dict(version_id=second['version_id']))
        binding={key:source[key] for key in ('version_id','access_revision','index_generation','content_digest')};task=None
        try:
            async with self.database.transaction() as connection:
                owner=(await (await connection.execute('SELECT pg_backend_pid()')).fetchone())[0]
                result=(await (await connection.execute(sql.SQL('SELECT {}.knowledge_call(%s,%s,%s)').format(sql.Identifier(self.schema)),(digest('session',person['token']),'source_recheck',Jsonb(binding)))).fetchone())[0]
                require(result.get('status')=='ok','source-final-buffer')
                task=asyncio.create_task(self.mutate(second,'withdraw'))
                async def source_writer_waiting():return await self.scalar('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid<>%s AND %s=ANY(pg_blocking_pids(pid)))',(owner,owner))
                await self.wait_for(source_writer_waiting,'withdraw-not-blocked-by-source-final-guard')
                require(not task.done(),'source-guard-released-early')
            require((await task).status_code==200 and (await self.source_http(second)).status_code==404,'source-postwithdraw-deny')
        finally:
            if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.passed('final_source_guard_holds_shared_locks_until_commit_then_withdraw_denies')

    async def active_ingestion_changes(self):
        h=self
        for operation in ('acl','withdraw'):
            row=await self.document('In progress claims policy.',ready=False);claim=await self.scheduler.claim();before=self.embedding.calls
            class ChangedDuringExtraction:
                ref='fixture:utf8-v1'
                async def extract(self,data,mime):
                    require((await h.mutate(row,operation,readers=[])).status_code==200,'inflight-mutation')
                    from enterprise_ai_platform.modules.knowledge.domain import Page
                    return (Page(1,data.decode()),)
            try:await self.process_knowledge(claim,extractor=ChangedDuringExtraction())
            except KnowledgeFailure:pass
            else:raise CheckFailure('inflight-'+operation+'-ready')
            require(self.embedding.calls==before and await self.scalar("SELECT state='held' FROM {s}.outbox_jobs WHERE id=%s",(UUID(row['version_id']),)),'inflight-held-'+operation)
            require(await self.scalar("SELECT index_state='failed' AND error_code='forbidden' FROM {s}.document_versions WHERE id=%s",(UUID(row['version_id']),)),'inflight-status-'+operation)
        self.passed('acl_or_withdraw_during_ingestion_holds_job_before_embedding_and_marks_unready_failed')

    async def collection_scope(self):
        tenant=self.people[0]['tenant'];collection=uuid4();other=uuid4()
        for value in (collection,other):await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,value))
        indexes=[]
        for value in (collection,other):
            person=uuid4();member=await self.member(tenant,person,grants=(RoleGrant('knowledge_manager','knowledge_collection',str(value)),));token,csrf=await self.session(tenant,person)
            indexes.append(len(self.people));self.people.append(dict(tenant=tenant,person=person,member=member,token=token,csrf=csrf))
        owner=indexes[0]
        async with await self.client() as client:
            response=await client.post('/api/v1/files',params=dict(purpose='knowledge',collection_id=str(collection)),content=b'Collection collectionclaims policy.',headers=self.headers(owner))
        require(response.status_code==202,'collection-file');await super(IngestHarness,self).process()
        created=await self.create_document(response.json()['id'],index=owner,readers=[str(self.people[4]['person'])]);require(created.status_code==202,'collection-document');row=created.json()
        await self.process_knowledge();require((await self.mutate(row,'publish',index=owner)).status_code==200,'collection-publish')
        require((await self.source_http(row,indexes[1])).status_code==404 and (await self.search_http('collectionclaims',indexes[1])).json()['items']==[],'other-collection-denied')
        require((await self.source_http(row,owner)).status_code==200,'exact-collection-allowed')
        self.passed('exact_collection_manager_allowed_other_collection_manager_denied')
        base=self.repo;h=self
        class Deactivated:
            async def call(self,token,operation,payload):
                result=await base.call(token,operation,payload)
                if operation=='search':await h.execute('UPDATE {s}.authorization_scopes SET active=false WHERE scope_ref=%s',(collection,))
                return result
        self.knowledge=KnowledgeService(Deactivated(),ORIGIN)
        try:
            require((await self.search_http('collectionclaims')).json()['items']==[],'inactive-collection-buffer')
            require((await self.source_http(row)).status_code==404,'inactive-collection-source')
        finally:self.knowledge=KnowledgeService(base,ORIGIN)
        self.passed('collection_deactivation_between_fetches_filters_index_and_source_without_reindex')

    async def publication_rollback(self):
        first=await self.document('Atomic claims original.');require((await self.mutate(first,'publish')).status_code==200,'rollback-original-published')
        second=await self.new_version(first,'Atomic claims replacement.');before=await self.current(second);key=str(uuid4())
        await self.execute("CREATE FUNCTION {s}.publish_failure_fixture() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Fixture failure' USING ERRCODE='23514'; END; $$")
        await self.execute('REVOKE ALL ON FUNCTION {s}.publish_failure_fixture() FROM PUBLIC,{a}')
        await self.execute("CREATE TRIGGER publish_failure_fixture BEFORE INSERT ON {s}.knowledge_events FOR EACH ROW WHEN (NEW.operation='publish') EXECUTE FUNCTION {s}.publish_failure_fixture()")
        try:require((await self.mutate(second,'publish',key=key)).status_code==400,'atomic-failure-response')
        finally:await self.execute('DROP TRIGGER publish_failure_fixture ON {s}.knowledge_events;DROP FUNCTION {s}.publish_failure_fixture()')
        after=await self.current(second)
        require(after['document_version']==before['document_version'] and after['active_version_id']==first['version_id'] and after['publication_state']=='draft','atomic-state-rollback')
        require((await self.source_http(first)).json()['publication_state']=='published','old-version-still-published')
        require(await self.scalar('SELECT count(*) FROM {s}.knowledge_events WHERE key=%s',(UUID(key),))==0,'audit-rollback')
        require((await self.mutate(second,'publish',key=key)).status_code==200,'same-key-recovery')
        self.passed('failed_publish_rolls_back_pointer_old_new_states_revision_and_audit')

    async def controlled_scope_mutation(self):
        tenant=self.people[0]['tenant'];collection=uuid4()
        await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)",(tenant,collection))
        async with await self.client() as client:
            response=await client.post('/api/v1/files',params=dict(purpose='knowledge',collection_id=str(collection)),content=b'Scope barrierscope claims policy.',headers=self.headers(0))
        require(response.status_code==202,'scope-barrier-file');await super(IngestHarness,self).process()
        created=await self.create_document(response.json()['id'],readers=[str(self.people[4]['person'])]);require(created.status_code==202,'scope-barrier-admission');row=created.json();await self.process_knowledge()
        file_id=await self.scalar('SELECT file_id FROM {s}.document_versions WHERE id=%s',(UUID(row['version_id']),));admin_pid=await self.scalar('SELECT pg_backend_pid()')
        publisher=None;deactivator=None;key=str(uuid4());publisher_pid=[]
        try:
            async with direct_connection(self.direct,timeout=30) as blocker:
                async with blocker.transaction():
                    await blocker.execute(sql.SQL('SELECT id FROM {}.files WHERE id=%s FOR UPDATE').format(sql.Identifier(self.schema)),(file_id,))
                    publisher=asyncio.create_task(self.mutate(row,'publish',key=key,expected=1))
                    async def publisher_waiting():
                        found=await (await blocker.execute("SELECT pid FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock'",('eai_runtime_'+self.schema.rsplit('_',1)[1],))).fetchone()
                        if found:publisher_pid.append(found[0]);return True
                        return False
                    await self.wait_for(publisher_waiting,'publisher-never-waited-on-file')
                    deactivator=asyncio.create_task(self.execute('UPDATE {s}.authorization_scopes SET active=false WHERE scope_ref=%s',(collection,)))
                    async def scope_writer_waiting():return (await (await blocker.execute('SELECT %s=ANY(pg_blocking_pids(%s))',(publisher_pid[0],admin_pid))).fetchone())[0]
                    await self.wait_for(scope_writer_waiting,'collection-deactivation-not-held-by-mutation')
                    require(not publisher.done() and not deactivator.done(),'scope-barrier-overlap')
            require((await publisher).status_code==200,'valid-publish-precedes-deactivation');await deactivator
        finally:
            for task in (publisher,deactivator):
                if task is not None and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        require((await self.source_http(row)).status_code==404 and (await self.search_http('barrierscope')).json()['items']==[],'post-deactivation-hidden')
        require((await self.mutate(row,'publish',key=key,expected=1)).status_code==404,'inactive-scope-replay-denied')
        self.passed('publish_scope_shared_lock_blocks_deactivation_until_commit_and_replay_reauthorizes')

    async def strict_and_private(self):
        row=await self.document('Strict claims policy.');person=self.people[0]
        base=dict(id=row['version_id'],expected_version=1,key=str(uuid4()),csrf_hash=digest('csrf',person['csrf']))
        for change in ({'expected_version':None},{'expected_version':True},{'expected_version':'1'},{'key':None},{'extra':1}):
            try:await self.repo.call(digest('session',person['token']),'publish',{**base,**change})
            except KnowledgeFailure as error:require(error.status==400,'strict-publish-status')
            else:raise CheckFailure('malformed-publish-accepted')
        self.passed('restricted_rpc_null_boolean_string_and_extra_mutations_rejected')
        require((await self.mutate(row,'publish')).status_code==200,'strict-ready-publish')
        try:
            async with self.admin.transaction():
                await self.execute("UPDATE {s}.document_versions SET publication_state='superseded' WHERE id=%s",(UUID(row['version_id']),))
        except Exception:pass
        else:raise CheckFailure('inconsistent-pointer-committed')
        require((await self.source_http(row)).json()['publication_state']=='published','pointer-rollback')
        self.passed('deferred_pointer_invariant_rejects_inconsistent_commit')
        async with self.database.transaction() as connection:
            try:await connection.execute(sql.SQL('SELECT * FROM {}.knowledge_events').format(sql.Identifier(self.schema)))
            except Exception:pass
            else:raise CheckFailure('events-exposed')
        await self.execute('GRANT SELECT ON {s}.knowledge_events TO {a}')
        try:
            try:await self.repo.call(digest('session',person['token']),'search',dict(query='claims',language='en',limit=1))
            except KnowledgeFailure:pass
            else:raise CheckFailure('privilege-drift-accepted')
        finally:await self.execute('REVOKE SELECT ON {s}.knowledge_events FROM {a}')
        self.passed('audit_table_private_and_runtime_privilege_drift_fails_closed')

async def verify(direct,base):
    schema='db03_probe_'+uuid4().hex;extension_schema='eai_vector_probe_'+uuid4().hex;owned_extension=False;owned=False;database=None;stage='extension'
    sources=source_hashes();before=await migrate(direct)
    try:
        async with direct_connection(direct) as connection:
            if not (await (await connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")).fetchone()):
                async with connection.transaction():
                    await connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extension_schema)))
                    await connection.execute(sql.SQL('CREATE EXTENSION vector WITH SCHEMA {}').format(sql.Identifier(extension_schema)))
                owned_extension=True
            require((await (await connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,))).fetchone()) is None,'namespace-unowned')
        stage='migration';await migrate(direct,schema=schema,upgrade=True);owned=True
        target=await provision_role(direct,base,schema=schema,role_name='eai_runtime_'+schema.rsplit('_',1)[1]);database=RuntimeDatabase(target,schema=schema);await database.open()
        async with direct_connection(direct,timeout=600) as connection:
            h=Harness(connection,database,schema,Extractor());h.direct=direct;await h.setup_lifecycle()
            for name in ('publication_and_visibility','acl_and_withdrawal','buffered_revocations','races','controlled_key_race','controlled_recheck_race','active_ingestion_changes','collection_scope','publication_rollback','controlled_scope_mutation','strict_and_private'):
                stage=name;await getattr(h,name)();print('Knowledge lifecycle checks completed: '+name,flush=True)
        require(source_hashes()==sources,'source-stable');require(await migrate(direct)==before,'platform-unchanged')
        return dict(passed=True,checks=h.checks,check_count=len(h.checks),source_sha256=sources,tested_revision=10,platform_revision=before[0],platform_schema_modified=False,
          parser_embedding_storage='controlled_fixtures_no_paid_or_cloud_calls',cleanup='completed')
    except Exception as error:raise CheckFailure(stage+':'+(str(error) if isinstance(error,CheckFailure) else type(error).__name__)) from None
    finally:
        if database is not None:await database.close()
        if owned:await drop_probe_schema(direct,schema)
        if owned_extension:
            async with direct_connection(direct) as connection:
                found=await (await connection.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'")).fetchone()
                require(found==(extension_schema,),'owned-extension-cleanup')
                await connection.execute('DROP EXTENSION vector');await connection.execute(sql.SQL('DROP SCHEMA {}').format(sql.Identifier(extension_schema)))

def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/know02/contract-live.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required.')
    try:
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base))
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'}))
    except Exception as error:print('Knowledge lifecycle verification failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None

if __name__=='__main__':main()
