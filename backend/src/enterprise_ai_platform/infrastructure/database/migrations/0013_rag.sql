-- RAG-01: private attempt ledger, authorized exact vector/lexical retrieval.
ALTER TABLE {schema}.document_versions ADD COLUMN valid_from date,ADD COLUMN valid_until date,
 ADD CHECK(valid_until IS NULL OR valid_from IS NULL OR valid_until>valid_from);
CREATE TABLE {schema}.rag_attempts (
 tenant_id uuid NOT NULL,run_id uuid NOT NULL,kind text NOT NULL CHECK(kind IN ('embedding','chat')),
 job_id uuid NOT NULL,generation bigint NOT NULL,epoch bigint NOT NULL,request_digest text NOT NULL CHECK(request_digest ~ '^[a-f0-9]{{64}}$'),
 input_tokens integer,output_tokens integer,state text NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','known','unknown')),
 PRIMARY KEY(tenant_id,run_id,kind),FOREIGN KEY(tenant_id,run_id) REFERENCES {schema}.runs(tenant_id,id),
 FOREIGN KEY(tenant_id,job_id) REFERENCES {schema}.outbox_jobs(tenant_id,id),
 CHECK((state='known' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND input_tokens>=0 AND output_tokens>=0) OR (state IN ('reserved','unknown') AND input_tokens IS NULL AND output_tokens IS NULL))
);
ALTER TABLE {schema}.rag_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.rag_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.rag_attempts TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.rag_attempts FROM PUBLIC,{access_role};

CREATE TABLE {schema}.rag_citations (
 tenant_id uuid NOT NULL,message_id uuid NOT NULL,conversation_id uuid NOT NULL,label text NOT NULL CHECK(label ~ '^S[1-5]$'),
 version_id uuid NOT NULL,index_generation bigint NOT NULL,chunk_no integer NOT NULL,page integer NOT NULL,text_digest text NOT NULL,quote text NOT NULL CHECK(length(quote) BETWEEN 1 AND 180),
 PRIMARY KEY(tenant_id,message_id,label),FOREIGN KEY(tenant_id,message_id,conversation_id) REFERENCES {schema}.messages(tenant_id,id,conversation_id),
 FOREIGN KEY(tenant_id,version_id,index_generation,chunk_no) REFERENCES {schema}.chunks(tenant_id,document_version_id,index_generation,chunk_no)
);
ALTER TABLE {schema}.rag_citations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.rag_citations FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.rag_citations TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.rag_citations FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.rag_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE citation jsonb;bound jsonb;context jsonb;r {schema}.runs%ROWTYPE;c {schema}.conversations%ROWTYPE;v {schema}.document_versions%ROWTYPE;d {schema}.documents%ROWTYPE;
 ch {schema}.chunks%ROWTYPE;t uuid;p uuid;item jsonb;items jsonb:='[]'::jsonb;result jsonb:='{{}}'::jsonb;
 refs jsonb;ns text;statement text;operation_kind text;mid uuid;sequence_no bigint;question text;prior text;deadline numeric;boundary bigint;rounds integer;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'RAG capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('input','search','recheck','guard','reserve','usage','publish','hold') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>65536
  OR NOT payload ?& ARRAY['credential_hash','tenant_id','run_id','job_id','generation','epoch']
  OR jsonb_typeof(payload->'epoch') IS DISTINCT FROM 'number' OR payload->>'epoch' !~ '^[1-9][0-9]{{0,17}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.checkpoint_call('stage',(payload-ARRAY['items','vector','query','mode','kind','request_digest','input_tokens','output_tokens','proposal','text','citations','refs'])||jsonb_build_object('sources','[]'::jsonb,'namespace','','checkpoint_id',NULL));
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 t:=(context->>'tenant_id')::uuid;p:=(context->>'principal_id')::uuid;deadline:=(context->>'authority_expires_at')::numeric;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=t AND id=(payload->>'run_id')::uuid;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=r.conversation_id;
 IF r.state<>'running' OR r.writer_generation<>(payload->>'epoch')::bigint OR r.checkpoint_job_id IS DISTINCT FROM (payload->>'job_id')::uuid OR r.checkpoint_job_generation IS DISTINCT FROM (payload->>'generation')::bigint THEN RETURN jsonb_build_object('status','stale'); END IF;
 IF operation='input' THEN
  SELECT body INTO question FROM {schema}.messages WHERE tenant_id=t AND id=r.input_message_id AND run_id=r.id AND author_kind='human' AND author_principal_id=p;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
  SELECT coalesce(max(sequence),0) INTO boundary FROM {schema}.messages WHERE tenant_id=t AND conversation_id=c.id AND author_kind='model' AND output_kind<>'clarification';
  SELECT count(*) INTO rounds FROM {schema}.messages WHERE tenant_id=t AND conversation_id=c.id AND sequence>boundary AND output_kind='clarification';
  SELECT string_agg(body,' ' ORDER BY sequence) INTO prior FROM {schema}.messages WHERE tenant_id=t AND conversation_id=c.id AND author_kind='human' AND sequence>boundary AND id<>r.input_message_id;
  result:=jsonb_build_object('question',question,'prior_question',CASE WHEN rounds BETWEEN 1 AND 2 THEN prior END,'clarification_rounds',rounds,'language',c.language);
 ELSIF operation='hold' THEN
  UPDATE {schema}.runs SET state='held',version=version+1,writer_generation=writer_generation+1,lease_until=NULL WHERE tenant_id=t AND id=r.id;
  context:={schema}.jobs_call('fail',jsonb_build_object('credential_hash',payload->>'credential_hash','id',r.job_id,'generation',r.checkpoint_job_generation,'error_code','forbidden'));
  IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'RAG hold fenced.'; END IF;
 ELSIF operation='search' THEN
  IF jsonb_typeof(payload->'mode') IS DISTINCT FROM 'string' OR payload->>'mode' NOT IN ('vector','hybrid','rerank') OR jsonb_typeof(payload->'query') IS DISTINCT FROM 'string' OR length(payload->>'query') NOT BETWEEN 1 AND 200
   OR jsonb_typeof(payload->'vector') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'vector')<>1024 THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(payload->'vector') x WHERE jsonb_typeof(x.value) IS DISTINCT FROM 'number' OR abs((x.value::text)::numeric)>1000000) THEN RETURN jsonb_build_object('status','invalid'); END IF;
  SELECT n.nspname INTO ns FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector';
  statement:=format($q$
   WITH authorized AS MATERIALIZED (
    SELECT dc.id doc,dv.id version_id,dv.version_no,dc.access_revision,dc.title,dv.content_digest,cc.index_generation,cc.chunk_no,cc.page,cc.start_offset,cc.end_offset,cc.text,cc.text_digest,
     ts_rank_cd(to_tsvector('simple'::regconfig,cc.text),websearch_to_tsquery('simple'::regconfig,$3)) lexical,
     cc.embedding OPERATOR(%1$I.<=>) (($4::text)::%1$I.vector) distance
    FROM {schema}.documents dc JOIN {schema}.document_versions dv ON dv.tenant_id=dc.tenant_id AND dv.id=dc.active_version_id
    JOIN {schema}.chunks cc ON cc.tenant_id=dv.tenant_id AND cc.document_version_id=dv.id AND cc.index_generation=dv.index_generation
    WHERE dc.tenant_id=$1 AND {schema}._knowledge_readable($1,$2,dv.id,false)
     AND (dv.valid_from IS NULL OR dv.valid_from<=(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date)
     AND (dv.valid_until IS NULL OR dv.valid_until>(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date)),
   ranked AS(SELECT *,row_number() OVER(ORDER BY distance,version_id,chunk_no) vr,row_number() OVER(ORDER BY lexical DESC,version_id,chunk_no) lr FROM authorized),
   scored AS(SELECT *,1.0/(60+vr)+CASE WHEN lexical>0 THEN 1.0/(60+lr) ELSE 0 END fused FROM ranked),
   selected AS(SELECT * FROM scored ORDER BY CASE WHEN $5='vector' THEN distance END ASC,
     CASE WHEN $5='rerank' THEN (lexical>0)::integer END DESC,CASE WHEN $5<>'vector' THEN fused END DESC,version_id,chunk_no LIMIT 5)
   SELECT coalesce(jsonb_agg(jsonb_build_object('document_id',doc,'version_id',version_id,'version_no',version_no,'access_revision',access_revision,'index_generation',index_generation,
    'title',title,'content_digest',content_digest,'chunk_no',chunk_no,'page',page,'start',start_offset,'end',end_offset,'text',text,'text_digest',text_digest)), '[]'::jsonb) FROM selected
  $q$,ns);
  EXECUTE statement INTO items USING t,p,payload->>'query',payload->'vector',payload->>'mode';
  result:=jsonb_build_object('items',items);
 ELSE
  IF operation='guard' THEN
   IF jsonb_typeof(payload->'refs') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'refs')>8 THEN RETURN jsonb_build_object('status','invalid'); END IF;
   -- Checkpoint consumer guard pins precisely the requested current document refs.
   FOR item IN SELECT value FROM jsonb_array_elements(payload->'refs') LOOP
    IF item->>'system' IS DISTINCT FROM 'platform' OR item->>'kind' IS DISTINCT FROM 'document_version' OR item->>'relation' IS DISTINCT FROM 'read' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=t AND id=(item->>'resource_id')::uuid;
    SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=v.document_id FOR SHARE;
    PERFORM id FROM {schema}.document_versions WHERE tenant_id=t AND id=v.id FOR SHARE;
    PERFORM id FROM {schema}.files WHERE tenant_id=t AND id=v.file_id FOR SHARE;
    PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_kind='knowledge_collection' AND scope_ref=d.collection_id FOR SHARE;
    IF d.id IS NULL OR NOT {schema}._knowledge_readable(t,p,v.id,false) OR v.version_no::text IS DISTINCT FROM item->>'version' OR d.access_revision::text IS DISTINCT FROM item->>'observed_revision'
     OR (v.valid_from IS NOT NULL AND v.valid_from>(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date) OR (v.valid_until IS NOT NULL AND v.valid_until<=(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date)
     OR NOT EXISTS(SELECT 1 FROM {schema}.chunks WHERE tenant_id=t AND document_version_id=v.id AND index_generation=v.index_generation AND page=(item->>'page')::integer AND chunk_no=(item->>'chunk')::integer) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
   END LOOP;
  ELSE
   IF jsonb_typeof(payload->'items') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'items')>5 THEN RETURN jsonb_build_object('status','invalid'); END IF;
   -- Canonical lock order matches knowledge lifecycle and final reads.
   PERFORM dd.id FROM {schema}.documents dd WHERE dd.tenant_id=t AND dd.id IN (SELECT vv.document_id FROM {schema}.document_versions vv JOIN jsonb_array_elements(payload->'items') x ON vv.id=(x.value->>'version_id')::uuid WHERE vv.tenant_id=t) ORDER BY dd.id FOR SHARE;
   PERFORM vv.id FROM {schema}.document_versions vv WHERE vv.tenant_id=t AND vv.id IN(SELECT (x.value->>'version_id')::uuid FROM jsonb_array_elements(payload->'items') x) ORDER BY vv.id FOR SHARE;
   PERFORM ff.id FROM {schema}.files ff WHERE ff.tenant_id=t AND ff.id IN(SELECT vv.file_id FROM {schema}.document_versions vv JOIN jsonb_array_elements(payload->'items') x ON vv.id=(x.value->>'version_id')::uuid WHERE vv.tenant_id=t) ORDER BY ff.id FOR SHARE;
   PERFORM ss.scope_ref FROM {schema}.authorization_scopes ss WHERE ss.tenant_id=t AND ss.scope_kind='knowledge_collection' AND ss.scope_ref IN(SELECT dd.collection_id FROM {schema}.documents dd JOIN {schema}.document_versions vv ON vv.tenant_id=dd.tenant_id AND vv.document_id=dd.id JOIN jsonb_array_elements(payload->'items') x ON vv.id=(x.value->>'version_id')::uuid WHERE dd.tenant_id=t) ORDER BY ss.scope_ref FOR SHARE;
   FOR item IN SELECT value FROM jsonb_array_elements(payload->'items') LOOP
    SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=t AND id=(item->>'version_id')::uuid;
    SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=v.document_id;
    SELECT * INTO ch FROM {schema}.chunks WHERE tenant_id=t AND document_version_id=v.id AND index_generation=v.index_generation AND chunk_no=(item->>'chunk_no')::integer FOR SHARE;
    IF d.id IS NULL OR ch.document_version_id IS NULL OR NOT {schema}._knowledge_readable(t,p,v.id,false) OR d.access_revision IS DISTINCT FROM (item->>'access_revision')::bigint OR v.index_generation IS DISTINCT FROM (item->>'index_generation')::bigint OR ch.text_digest IS DISTINCT FROM item->>'text_digest'
     OR (v.valid_from IS NOT NULL AND v.valid_from>(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date) OR (v.valid_until IS NOT NULL AND v.valid_until<=(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date) THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    items:=items||jsonb_build_array(jsonb_build_object('version_id',v.id,'version_no',v.version_no,'access_revision',d.access_revision,'index_generation',v.index_generation,'chunk_no',ch.chunk_no,'page',ch.page,'text',ch.text,'text_digest',ch.text_digest));
   END LOOP;
   IF operation='reserve' THEN
    operation_kind:=payload->>'kind';
    IF operation_kind IS NULL OR operation_kind NOT IN ('embedding','chat') OR jsonb_typeof(payload->'request_digest') IS DISTINCT FROM 'string' OR payload->>'request_digest' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF EXISTS(SELECT 1 FROM {schema}.rag_attempts WHERE tenant_id=t AND run_id=r.id AND kind=payload->>'kind') THEN RETURN jsonb_build_object('status','held'); END IF;
    INSERT INTO {schema}.rag_attempts(tenant_id,run_id,kind,job_id,generation,epoch,request_digest) VALUES(t,r.id,operation_kind,r.job_id,r.checkpoint_job_generation,r.writer_generation,payload->>'request_digest');
   ELSIF operation='usage' THEN
    IF payload->>'input_tokens' IS NOT NULL AND (jsonb_typeof(payload->'input_tokens') IS DISTINCT FROM 'number' OR jsonb_typeof(payload->'output_tokens') IS DISTINCT FROM 'number' OR payload->>'input_tokens' !~ '^[0-9]{{1,7}}$' OR payload->>'output_tokens' !~ '^[0-9]{{1,7}}$') THEN RETURN jsonb_build_object('status','invalid'); END IF;
    UPDATE {schema}.rag_attempts SET state=CASE WHEN payload->>'input_tokens' IS NULL THEN 'unknown' ELSE 'known' END,input_tokens=(payload->>'input_tokens')::integer,output_tokens=(payload->>'output_tokens')::integer
     WHERE tenant_id=t AND run_id=r.id AND kind=payload->>'kind' AND job_id=r.job_id AND generation=r.checkpoint_job_generation AND epoch=r.writer_generation AND state='reserved';
    IF NOT FOUND THEN RETURN jsonb_build_object('status','conflict'); END IF;
   ELSIF operation='publish' THEN
    operation_kind:=payload->>'kind';
    IF operation_kind IS NULL OR operation_kind NOT IN ('answer','clarification','insufficient_evidence','source_conflict','service_unavailable') OR jsonb_typeof(payload->'text') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'RAG publication rejected.' USING ERRCODE='23514'; END IF;
    IF operation_kind IN ('answer','source_conflict') AND jsonb_array_length(items)=0 THEN RAISE EXCEPTION 'RAG publication rejected.' USING ERRCODE='23514'; END IF;
    mid:=gen_random_uuid();sequence_no:=c.next_sequence;
    INSERT INTO {schema}.messages(tenant_id,id,conversation_id,author_kind,body,sequence,run_id,output_kind,provenance_mode) VALUES(t,mid,c.id,'model',payload->>'text',sequence_no,r.id,operation_kind,CASE WHEN jsonb_array_length(items)>0 THEN 'dependent' ELSE 'source_free' END);
    FOR item IN SELECT value FROM jsonb_array_elements(items) LOOP
     IF NOT EXISTS(SELECT 1 FROM {schema}.source_dependencies WHERE tenant_id=t AND run_id=r.id AND source_system='platform' AND resource_type='document_version' AND resource_id=(item->>'version_id')::uuid AND resource_version=item->>'version_no' AND observed_revision=item->>'access_revision' AND chunk=(item->>'chunk_no')::integer AND page=(item->>'page')::integer) THEN
      INSERT INTO {schema}.source_dependencies(tenant_id,id,conversation_id,run_id,source_system,resource_type,resource_id,resource_version,required_relation,observed_revision,page,chunk,platform_document_version_id)
       VALUES(t,gen_random_uuid(),c.id,r.id,'platform','document_version',(item->>'version_id')::uuid,item->>'version_no','read',item->>'access_revision',(item->>'page')::integer,(item->>'chunk_no')::integer,(item->>'version_id')::uuid);
     END IF;
    END LOOP;
    IF jsonb_typeof(payload->'citations') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'citations')>2 THEN RAISE EXCEPTION 'RAG publication rejected.' USING ERRCODE='23514'; END IF;
    FOR citation IN SELECT value FROM jsonb_array_elements(payload->'citations') LOOP
     IF citation->>'id' IS NULL OR citation->>'id' !~ '^S[1-5]$' THEN RAISE EXCEPTION 'RAG publication rejected.' USING ERRCODE='23514'; END IF;
     bound:=items->(substring(citation->>'id' FROM 2)::integer-1);
     IF bound IS NULL OR citation->>'version_id' IS DISTINCT FROM bound->>'version_id' OR citation->>'text_digest' IS DISTINCT FROM bound->>'text_digest' OR citation->>'chunk_no' IS DISTINCT FROM bound->>'chunk_no' OR citation->>'page' IS DISTINCT FROM bound->>'page'
      OR jsonb_typeof(citation->'quote') IS DISTINCT FROM 'string' OR position(citation->>'quote' in bound->>'text')=0 THEN RAISE EXCEPTION 'RAG publication rejected.' USING ERRCODE='23514'; END IF;
     INSERT INTO {schema}.rag_citations VALUES(t,mid,c.id,citation->>'id',(bound->>'version_id')::uuid,(bound->>'index_generation')::bigint,(bound->>'chunk_no')::integer,(bound->>'page')::integer,bound->>'text_digest',citation->>'quote');
    END LOOP;
    UPDATE {schema}.conversations SET next_sequence=next_sequence+1 WHERE tenant_id=t AND id=c.id;
    context:={schema}.checkpoint_call('finish',(payload-ARRAY['items','kind','text','citations'])||jsonb_build_object('sources','[]'::jsonb,'namespace','','checkpoint_id',NULL,'state','completed'));
    IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'RAG publication fenced.'; END IF;
    result:=jsonb_build_object('message_id',mid,'kind',operation_kind);
   END IF;
  END IF;
 END IF;
 IF extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'RAG authority expired.'; END IF;
 RETURN result||jsonb_build_object('status','ok','items',items,'authority_expires_at',deadline);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation OR foreign_key_violation THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.rag_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.rag_call(text,jsonb) TO {access_role};

CREATE OR REPLACE FUNCTION {schema}._runtime_role_is_valid(candidate oid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles r
        JOIN {schema}.runtime_role_registry b ON b.role_oid=r.oid AND b.role_name=r.rolname
        CROSS JOIN {schema}.runtime_access_role_registry binding
        JOIN pg_catalog.pg_roles a ON a.oid=binding.role_oid AND a.rolname=binding.role_name
        WHERE r.oid=candidate AND r.rolcanlogin
          AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole
                   OR r.rolreplication OR r.rolinherit)
          AND a.oid='{access_role}'::regrole AND NOT a.rolcanlogin
          AND NOT (a.rolsuper OR a.rolbypassrls OR a.rolcreatedb OR a.rolcreaterole OR a.rolreplication OR a.rolinherit)
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members am WHERE am.member=a.oid)
          AND (SELECT count(*) FROM pg_catalog.pg_auth_members am WHERE am.member=r.oid)=1
          AND EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members am WHERE am.member=r.oid
                      AND am.roleid=a.oid AND am.inherit_option AND NOT am.set_option AND NOT am.admin_option)
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relowner IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspowner IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_database d WHERE d.datdba IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_proc f WHERE f.proowner IN (r.oid,a.oid))
          AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_type t WHERE t.typowner IN (r.oid,a.oid))
          AND NOT has_database_privilege(r.oid,current_database(),'CREATE')
          AND NOT has_database_privilege(r.oid,current_database(),'CONNECT WITH GRANT OPTION')
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_namespace n
              WHERE n.nspname !~ '^pg_' AND has_schema_privilege(r.oid,n.oid,'CREATE')
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND has_any_column_privilege(r.oid,c.oid,'SELECT')
          )
          AND has_schema_privilege(r.oid, (SELECT relnamespace FROM pg_catalog.pg_class
              WHERE oid='{schema}.runtime_role_registry'::regclass), 'USAGE')
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('principals','tenants','memberships','membership_roles','tenant_configs','timezone_catalog')
                AND NOT has_table_privilege(r.oid,c.oid,'SELECT')
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c,
                  LATERAL pg_catalog.aclexplode(c.relacl) acl
              WHERE c.relnamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND acl.grantee IN (r.oid,a.oid,0) AND acl.is_grantable
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_attribute att
              JOIN pg_catalog.pg_class c ON c.oid=att.attrelid,
                  LATERAL pg_catalog.aclexplode(att.attacl) acl
              WHERE c.relnamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND acl.grantee IN (r.oid,a.oid,0) AND acl.is_grantable
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_proc f
              WHERE f.pronamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND (has_function_privilege(r.oid,f.oid,'EXECUTE WITH GRANT OPTION')
                    OR (f.proname NOT IN ('context_actor_id','context_tenant_id','has_active_context','runtime_role_is_valid')
                        AND NOT (f.proname='identity_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='authorization_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='checkpoint_owner_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='rag_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='checkpoint_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='conversation_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='knowledge_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='files_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='jobs_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='delegation_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND has_function_privilege(r.oid,f.oid,'EXECUTE')))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_sequence seq JOIN pg_catalog.pg_class c ON c.oid=seq.seqrelid
              WHERE c.relnamespace=(SELECT relnamespace FROM pg_catalog.pg_class
                    WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND has_sequence_privilege(r.oid,seq.seqrelid,'USAGE,SELECT,UPDATE')
          )
          AND NOT has_schema_privilege(r.oid,(SELECT relnamespace FROM pg_catalog.pg_class
              WHERE oid='{schema}.runtime_role_registry'::regclass),'USAGE WITH GRANT OPTION')
    );
$$;



CREATE OR REPLACE FUNCTION {schema}._conversation_message(m {schema}.messages) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 WITH value AS (SELECT jsonb_build_object('id',m.id,'sequence',m.sequence,'author_kind',m.author_kind,'body',m.body,'run_id',m.run_id,'output_kind',m.output_kind,'provenance_mode',m.provenance_mode,'created_at',m.created_at,
  'citations',coalesce((SELECT jsonb_agg(jsonb_build_object('id',rc.label,'version_id',rc.version_id,'page',rc.page,'chunk_no',rc.chunk_no,'text_digest',rc.text_digest,'quote',rc.quote) ORDER BY rc.label) FROM {schema}.rag_citations rc WHERE rc.tenant_id=m.tenant_id AND rc.message_id=m.id),'[]'::jsonb),
  'feedback',(SELECT jsonb_build_object('rating',f.rating,'reason',f.reason,'version',f.version) FROM {schema}.feedback f WHERE f.tenant_id=m.tenant_id AND f.message_id=m.id),
  '_sources',coalesce((SELECT jsonb_agg(jsonb_build_object('system',d.source_system,'kind',d.resource_type,'resource_id',d.resource_id,'version',d.resource_version,'relation',d.required_relation,'observed_revision',d.observed_revision,'page',d.page,'chunk',d.chunk) ORDER BY d.id)
    FROM {schema}.source_dependencies d WHERE d.tenant_id=m.tenant_id AND d.conversation_id=m.conversation_id AND (d.output_message_id=m.id OR (m.author_kind<>'human' AND d.run_id=m.run_id))),'[]'::jsonb)) AS item)
 SELECT item||jsonb_build_object('_snapshot',encode(sha256(convert_to(item::text,'UTF8')),'hex')) FROM value;
$$;
