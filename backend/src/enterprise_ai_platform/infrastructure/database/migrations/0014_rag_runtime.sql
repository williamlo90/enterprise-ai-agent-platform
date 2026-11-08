-- RAG-02: durable budgets, metadata-only tracing/usage, live document history.
ALTER TABLE {schema}.runs ADD COLUMN recovered_at timestamptz,ADD COLUMN recovery_actor_id uuid REFERENCES {schema}.principals(id);
CREATE TABLE {schema}.rag_runtime (
 tenant_id uuid NOT NULL,run_id uuid NOT NULL,started_at timestamptz NOT NULL DEFAULT statement_timestamp(),
 deadline timestamptz NOT NULL DEFAULT statement_timestamp()+interval '60 seconds',steps integer NOT NULL DEFAULT 0 CHECK(steps BETWEEN 0 AND 8),
 max_input_tokens integer NOT NULL DEFAULT 4608 CHECK(max_input_tokens=4608),max_output_tokens integer NOT NULL DEFAULT 256 CHECK(max_output_tokens=256),
 PRIMARY KEY(tenant_id,run_id),FOREIGN KEY(tenant_id,run_id) REFERENCES {schema}.runs(tenant_id,id),CHECK(deadline<=started_at+interval '60 seconds')
);
ALTER TABLE {schema}.rag_attempts ADD COLUMN reserved_input_tokens integer,ADD COLUMN reserved_output_tokens integer,
 ADD COLUMN settlement_hash text CHECK(settlement_hash ~ '^[a-f0-9]{{64}}$'),ADD COLUMN settlement_until timestamptz;
UPDATE {schema}.rag_attempts SET reserved_input_tokens=CASE WHEN kind='embedding' THEN 512 ELSE 4096 END,reserved_output_tokens=CASE WHEN kind='embedding' THEN 0 ELSE 256 END;
ALTER TABLE {schema}.rag_attempts ALTER COLUMN reserved_input_tokens SET NOT NULL,ALTER COLUMN reserved_output_tokens SET NOT NULL,
 ADD CHECK(reserved_input_tokens=CASE WHEN kind='embedding' THEN 512 ELSE 4096 END),ADD CHECK(reserved_output_tokens=CASE WHEN kind='embedding' THEN 0 ELSE 256 END);
CREATE TABLE {schema}.rag_events (
 tenant_id uuid NOT NULL,run_id uuid NOT NULL,sequence bigint NOT NULL,phase text NOT NULL CHECK(phase IN ('begin','retrieve','generate','provider','publish','hold','recover')),
 outcome text NOT NULL CHECK(outcome IN ('started','completed','failed','deadline','cancelled','budget','unknown','closed_without_replay')),
 duration_ms integer NOT NULL CHECK(duration_ms BETWEEN 0 AND 3600000),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,run_id,sequence),FOREIGN KEY(tenant_id,run_id) REFERENCES {schema}.runs(tenant_id,id)
);
ALTER TABLE {schema}.rag_runtime ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.rag_runtime FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.rag_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.rag_events FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.rag_runtime TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.rag_events TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.rag_runtime,{schema}.rag_events FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.document_sources_call(auth jsonb,refs jsonb,history boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;deadline numeric;item jsonb;decisions jsonb:='[]'::jsonb;
 v {schema}.document_versions%ROWTYPE;d {schema}.documents%ROWTYPE;allowed boolean;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Source capability rejected.'; END IF;
 IF jsonb_typeof(auth) IS DISTINCT FROM 'object' OR jsonb_typeof(refs) IS DISTINCT FROM 'array' OR jsonb_array_length(refs)>320 OR octet_length(refs::text)>131072 OR history IS NULL THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF auth ? 'token' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(auth))<>1 THEN RETURN jsonb_build_object('status','invalid'); END IF;
  context:={schema}.authorization_call(auth->>'token','context','{{}}'::jsonb);
  IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
  identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
  SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=auth->>'token';
 ELSE
  IF history OR (SELECT count(*) FROM jsonb_object_keys(auth))<>6 THEN RETURN jsonb_build_object('status','forbidden'); END IF;
  context:={schema}.checkpoint_call('stage',auth||jsonb_build_object('sources','[]'::jsonb,'namespace','','checkpoint_id',NULL));
  IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
  t:=(context->>'tenant_id')::uuid;p:=(context->>'principal_id')::uuid;deadline:=(context->>'authority_expires_at')::numeric;
 END IF;
 -- Lock the authorized tenant's objects in canonical order; never open another session.
 PERFORM dd.id FROM {schema}.documents dd WHERE dd.tenant_id=t AND dd.id IN(SELECT vv.document_id FROM {schema}.document_versions vv JOIN jsonb_array_elements(refs) x ON vv.id=(x.value->>'resource_id')::uuid WHERE vv.tenant_id=t AND x.value->>'system'='platform' AND x.value->>'kind'='document_version') ORDER BY dd.id FOR SHARE;
 PERFORM vv.id FROM {schema}.document_versions vv WHERE vv.tenant_id=t AND vv.id IN(SELECT (x.value->>'resource_id')::uuid FROM jsonb_array_elements(refs) x WHERE x.value->>'system'='platform' AND x.value->>'kind'='document_version') ORDER BY vv.id FOR SHARE;
 PERFORM ff.id FROM {schema}.files ff WHERE ff.tenant_id=t AND ff.id IN(SELECT vv.file_id FROM {schema}.document_versions vv JOIN jsonb_array_elements(refs) x ON vv.id=(x.value->>'resource_id')::uuid WHERE vv.tenant_id=t AND x.value->>'system'='platform' AND x.value->>'kind'='document_version') ORDER BY ff.id FOR SHARE;
 PERFORM ss.scope_ref FROM {schema}.authorization_scopes ss WHERE ss.tenant_id=t AND ss.scope_kind='knowledge_collection' AND ss.scope_ref IN(SELECT dd.collection_id FROM {schema}.documents dd JOIN {schema}.document_versions vv ON vv.tenant_id=dd.tenant_id AND vv.document_id=dd.id JOIN jsonb_array_elements(refs) x ON vv.id=(x.value->>'resource_id')::uuid WHERE dd.tenant_id=t AND x.value->>'system'='platform' AND x.value->>'kind'='document_version') ORDER BY ss.scope_ref FOR SHARE;
 FOR item IN SELECT value FROM jsonb_array_elements(refs) LOOP
  allowed:=false;
  IF item->>'system'='platform' AND item->>'kind'='document_version' AND item->>'relation'='read' THEN
   SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=t AND id=(item->>'resource_id')::uuid;
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=v.document_id;
   allowed:=d.id IS NOT NULL AND {schema}._knowledge_readable(t,p,v.id,history) AND v.version_no::text IS NOT DISTINCT FROM item->>'version'
    AND (history OR (d.access_revision::text IS NOT DISTINCT FROM item->>'observed_revision' AND (v.valid_from IS NULL OR v.valid_from<=(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date) AND (v.valid_until IS NULL OR v.valid_until>(clock_timestamp() AT TIME ZONE 'Asia/Singapore')::date)))
    AND ((item->>'page' IS NULL AND item->>'chunk' IS NULL) OR EXISTS(SELECT 1 FROM {schema}.chunks WHERE tenant_id=t AND document_version_id=v.id AND index_generation=v.index_generation AND page=(item->>'page')::integer AND chunk_no=(item->>'chunk')::integer));
  END IF;
  decisions:=decisions||jsonb_build_array(coalesce(allowed,false));
 END LOOP;
 IF extract(epoch FROM clock_timestamp())>=deadline THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 RETURN jsonb_build_object('status','ok','tenant_id',t,'principal_id',p,'decisions',decisions,'authority_expires_at',deadline);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.document_sources_call(jsonb,jsonb,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.document_sources_call(jsonb,jsonb,boolean) TO {access_role};

ALTER FUNCTION {schema}.rag_call(text,jsonb) RENAME TO _rag_core;
REVOKE ALL ON FUNCTION {schema}._rag_core(text,jsonb) FROM PUBLIC,{access_role};
CREATE OR REPLACE FUNCTION {schema}._rag_core(operation text,payload jsonb) RETURNS jsonb
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
    INSERT INTO {schema}.rag_attempts(tenant_id,run_id,kind,job_id,generation,epoch,request_digest,reserved_input_tokens,reserved_output_tokens) VALUES(t,r.id,operation_kind,r.job_id,r.checkpoint_job_generation,r.writer_generation,payload->>'request_digest',CASE WHEN operation_kind='embedding' THEN 512 ELSE 4096 END,CASE WHEN operation_kind='embedding' THEN 0 ELSE 256 END);
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

CREATE FUNCTION {schema}.rag_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE result jsonb;b {schema}.rag_runtime%ROWTYPE;receipt text;total_in bigint;total_out bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'RAG capability rejected.'; END IF;
 -- Core authorization locks the exact job/run before budget state is inspected.
 result:={schema}._rag_core('recheck',(payload-ARRAY['kind','request_digest','input_tokens','output_tokens','text','citations','query','vector','mode'])||jsonb_build_object('items','[]'::jsonb));
 IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
 SELECT * INTO b FROM {schema}.rag_runtime WHERE tenant_id=(payload->>'tenant_id')::uuid AND run_id=(payload->>'run_id')::uuid FOR UPDATE;
 IF operation NOT IN ('hold','usage') AND (b.run_id IS NULL OR clock_timestamp()>=b.deadline OR b.steps>8) THEN RETURN jsonb_build_object('status','held'); END IF;
 IF operation NOT IN ('hold','usage') AND EXISTS(SELECT 1 FROM {schema}.rag_attempts WHERE tenant_id=b.tenant_id AND run_id=b.run_id AND state='known' AND (input_tokens>reserved_input_tokens OR output_tokens>reserved_output_tokens)) THEN RETURN jsonb_build_object('status','capacity'); END IF;
 IF operation='reserve' THEN
  SELECT coalesce(sum(reserved_input_tokens),0),coalesce(sum(reserved_output_tokens),0) INTO total_in,total_out FROM {schema}.rag_attempts WHERE tenant_id=b.tenant_id AND run_id=b.run_id;
  IF total_in+(CASE WHEN payload->>'kind'='embedding' THEN 512 ELSE 4096 END)>b.max_input_tokens OR total_out+(CASE WHEN payload->>'kind'='embedding' THEN 0 ELSE 256 END)>b.max_output_tokens THEN RETURN jsonb_build_object('status','capacity'); END IF;
 END IF;
 result:={schema}._rag_core(operation,payload);
 IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
 IF operation='reserve' THEN
  receipt:=encode(sha256(convert_to(gen_random_uuid()::text||gen_random_uuid()::text,'UTF8')),'hex');
  UPDATE {schema}.rag_attempts SET reserved_input_tokens=CASE WHEN kind='embedding' THEN 512 ELSE 4096 END,reserved_output_tokens=CASE WHEN kind='embedding' THEN 0 ELSE 256 END,settlement_hash=encode(sha256(convert_to(receipt,'UTF8')),'hex'),settlement_until=clock_timestamp()+interval '1 hour'
   WHERE tenant_id=b.tenant_id AND run_id=b.run_id AND kind=payload->>'kind';
  result:=result||jsonb_build_object('settlement_receipt',receipt);
 END IF;
 IF operation NOT IN ('hold','usage') AND clock_timestamp()>=b.deadline THEN RAISE EXCEPTION 'Run budget expired.' USING ERRCODE='23514'; END IF;
 IF operation='publish' THEN
  INSERT INTO {schema}.rag_events(tenant_id,run_id,sequence,phase,outcome,duration_ms) SELECT b.tenant_id,b.run_id,coalesce(max(sequence),0)+1,'publish','completed',least(3600000,greatest(0,(extract(epoch FROM clock_timestamp()-b.started_at)*1000)::integer)) FROM {schema}.rag_events WHERE tenant_id=b.tenant_id AND run_id=b.run_id HAVING coalesce(max(sequence),0)<20;
 END IF;
 IF operation NOT IN ('hold','usage') THEN result:=result||jsonb_build_object('authority_expires_at',least((result->>'authority_expires_at')::numeric,extract(epoch FROM b.deadline))); END IF;
 RETURN result;
EXCEPTION WHEN check_violation OR not_null_violation OR invalid_text_representation THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.rag_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.rag_call(text,jsonb) TO {access_role};

CREATE FUNCTION {schema}.rag_runtime_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE result jsonb;b {schema}.rag_runtime%ROWTYPE;next_sequence bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Runtime capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('begin','step','event') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>2048 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 result:={schema}._rag_core('recheck',(payload-ARRAY['phase','outcome','duration_ms'])||jsonb_build_object('items','[]'::jsonb));
 IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
 IF operation='begin' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>6 THEN RETURN jsonb_build_object('status','invalid'); END IF;
  INSERT INTO {schema}.rag_runtime(tenant_id,run_id) VALUES((payload->>'tenant_id')::uuid,(payload->>'run_id')::uuid) ON CONFLICT DO NOTHING;
 END IF;
 SELECT * INTO b FROM {schema}.rag_runtime WHERE tenant_id=(payload->>'tenant_id')::uuid AND run_id=(payload->>'run_id')::uuid FOR UPDATE;
 IF b.run_id IS NULL THEN RETURN jsonb_build_object('status','held'); END IF;
 IF operation IN ('begin','step') AND (clock_timestamp()>=b.deadline OR b.steps>=8 OR EXISTS(SELECT 1 FROM {schema}.rag_attempts WHERE tenant_id=b.tenant_id AND run_id=b.run_id AND state='known' AND (input_tokens>reserved_input_tokens OR output_tokens>reserved_output_tokens))) THEN RETURN jsonb_build_object('status','held'); END IF;
 IF operation='step' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>7 OR payload->>'phase' NOT IN ('retrieve','generate') OR jsonb_typeof(payload->'phase') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  UPDATE {schema}.rag_runtime SET steps=steps+1 WHERE tenant_id=b.tenant_id AND run_id=b.run_id RETURNING * INTO b;
 ELSIF operation='event' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>9 OR jsonb_typeof(payload->'phase') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'outcome') IS DISTINCT FROM 'string'
   OR payload->>'phase' NOT IN ('begin','retrieve','generate','provider','publish','hold') OR payload->>'outcome' NOT IN ('started','completed','failed','deadline','cancelled','budget','unknown')
   OR jsonb_typeof(payload->'duration_ms') IS DISTINCT FROM 'number' OR payload->>'duration_ms' !~ '^[0-9]{{1,7}}$' OR (payload->>'duration_ms')::integer>3600000 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 END IF;
 SELECT coalesce(max(sequence),0)+1 INTO next_sequence FROM {schema}.rag_events WHERE tenant_id=b.tenant_id AND run_id=b.run_id;
 IF next_sequence<=20 THEN
  INSERT INTO {schema}.rag_events(tenant_id,run_id,sequence,phase,outcome,duration_ms) VALUES(b.tenant_id,b.run_id,next_sequence,CASE WHEN operation='begin' THEN 'begin' ELSE payload->>'phase' END,CASE WHEN operation='event' THEN payload->>'outcome' ELSE 'started' END,CASE WHEN operation='event' THEN (payload->>'duration_ms')::integer ELSE 0 END);
 END IF;
 RETURN result||jsonb_build_object('deadline',extract(epoch FROM b.deadline),'max_steps',8,'steps',b.steps,'max_input_tokens',b.max_input_tokens,'max_output_tokens',b.max_output_tokens);
END;
$$;
REVOKE ALL ON FUNCTION {schema}.rag_runtime_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.rag_runtime_call(text,jsonb) TO {access_role};

CREATE FUNCTION {schema}.rag_usage_call(payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE a {schema}.rag_attempts%ROWTYPE;known boolean;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Usage capability rejected.'; END IF;
 IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR NOT payload ?& ARRAY['tenant_id','run_id','kind','receipt','input_tokens','output_tokens']
  OR jsonb_typeof(payload->'receipt') IS DISTINCT FROM 'string' OR payload->>'receipt' !~ '^[a-f0-9]{{64}}$' OR jsonb_typeof(payload->'kind') IS DISTINCT FROM 'string' OR payload->>'kind' NOT IN ('embedding','chat') THEN RETURN jsonb_build_object('status','invalid'); END IF;
 SELECT * INTO a FROM {schema}.rag_attempts WHERE tenant_id=(payload->>'tenant_id')::uuid AND run_id=(payload->>'run_id')::uuid AND kind=payload->>'kind' FOR UPDATE;
 IF NOT FOUND OR a.settlement_hash IS DISTINCT FROM encode(sha256(convert_to(payload->>'receipt','UTF8')),'hex') OR a.settlement_until IS NULL OR clock_timestamp()>=a.settlement_until THEN RETURN jsonb_build_object('status','forbidden'); END IF;
 known:=payload->'input_tokens'<>'null'::jsonb;
 IF known THEN
  IF jsonb_typeof(payload->'input_tokens') IS DISTINCT FROM 'number' OR jsonb_typeof(payload->'output_tokens') IS DISTINCT FROM 'number' OR payload->>'input_tokens' !~ '^[0-9]{{1,7}}$' OR payload->>'output_tokens' !~ '^[0-9]{{1,7}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
 ELSIF payload->'output_tokens'<>'null'::jsonb THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF a.state<>'reserved' THEN
  IF a.input_tokens IS DISTINCT FROM (payload->>'input_tokens')::integer OR a.output_tokens IS DISTINCT FROM (payload->>'output_tokens')::integer THEN RETURN jsonb_build_object('status','conflict'); END IF;
 ELSE
  UPDATE {schema}.rag_attempts SET state=CASE WHEN known THEN 'known' ELSE 'unknown' END,input_tokens=(payload->>'input_tokens')::integer,output_tokens=(payload->>'output_tokens')::integer WHERE tenant_id=a.tenant_id AND run_id=a.run_id AND kind=a.kind;
 END IF;
 RETURN jsonb_build_object('status','ok');
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR check_violation THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.rag_usage_call(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.rag_usage_call(jsonb) TO {access_role};

CREATE FUNCTION {schema}.rag_owner_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;r {schema}.runs%ROWTYPE;j {schema}.outbox_jobs%ROWTYPE;b {schema}.rag_runtime%ROWTYPE;events jsonb;usage jsonb;n bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Diagnostics capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('diagnostics','recover') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>1024 OR NOT payload ? 'id' THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN operation='recover' THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=t AND id=(payload->>'id')::uuid AND actor_id=p;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM {schema}.membership_roles WHERE tenant_id=t AND membership_id=(identity->>'active_membership_id')::uuid AND role='employee' AND scope_kind='tenant' AND scope_ref=t::text) THEN RETURN jsonb_build_object('status','missing'); END IF;
 -- Match executor order: identity -> job -> conversation -> run. NOWAIT avoids
 -- waiting behind a still-live executor while holding owner identity locks.
 SELECT * INTO j FROM {schema}.outbox_jobs WHERE tenant_id=t AND id=r.job_id FOR UPDATE NOWAIT;
 PERFORM id FROM {schema}.conversations WHERE tenant_id=t AND id=r.conversation_id AND owner_principal_id=p FOR UPDATE;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=t AND id=r.id FOR UPDATE;
 IF operation='recover' THEN
  IF j.job_type<>'agent.resume' OR r.prompt_version IS DISTINCT FROM 'rag-extractive-v1' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF r.version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
  IF r.state NOT IN ('held','running','queued') OR (j.state='running' AND j.lease_until>clock_timestamp()) OR (r.state='queued' AND j.state IN ('queued','retry')) THEN RETURN jsonb_build_object('status','conflict'); END IF;
  UPDATE {schema}.runs SET state='failed',version=version+1,writer_generation=writer_generation+1,lease_until=NULL,recovered_at=clock_timestamp(),recovery_actor_id=p WHERE tenant_id=t AND id=r.id RETURNING * INTO r;
  UPDATE {schema}.conversations SET active_run_id=NULL,version=version+1 WHERE tenant_id=t AND id=r.conversation_id AND active_run_id=r.id;
  UPDATE {schema}.outbox_jobs SET state='held',worker_id=NULL,lease_until=NULL,attempt_deadline=NULL,error_code='authority_changed' WHERE tenant_id=t AND id=j.id;
  SELECT coalesce(max(sequence),0)+1 INTO n FROM {schema}.rag_events WHERE tenant_id=t AND run_id=r.id;
  IF n<=20 THEN INSERT INTO {schema}.rag_events(tenant_id,run_id,sequence,phase,outcome,duration_ms) VALUES(t,r.id,n,'recover','closed_without_replay',0); END IF;
 ELSE
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 END IF;
 SELECT * INTO b FROM {schema}.rag_runtime WHERE tenant_id=t AND run_id=r.id;
 SELECT coalesce(jsonb_agg(jsonb_build_object('phase',phase,'outcome',outcome,'duration_ms',duration_ms,'sequence',sequence) ORDER BY sequence),'[]'::jsonb) INTO events FROM {schema}.rag_events WHERE tenant_id=t AND run_id=r.id AND created_at>=clock_timestamp()-interval '7 days';
 SELECT jsonb_build_object('known_input_tokens',coalesce(sum(input_tokens) FILTER(WHERE state='known'),0),'known_output_tokens',coalesce(sum(output_tokens) FILTER(WHERE state='known'),0),
  'reserved_input_tokens',coalesce(sum(reserved_input_tokens),0),'reserved_output_tokens',coalesce(sum(reserved_output_tokens),0),'unknown_attempts',count(*) FILTER(WHERE state<>'known'),'attempts',count(*),'effective_input_tokens',coalesce(sum(greatest(coalesce(input_tokens,0),reserved_input_tokens)),0),'effective_output_tokens',coalesce(sum(greatest(coalesce(output_tokens,0),reserved_output_tokens)),0),'overrun',coalesce(bool_or(state='known' AND (input_tokens>reserved_input_tokens OR output_tokens>reserved_output_tokens)),false),'rate_card','prototype-nova-apac-cohere-sg-v1','known_estimate_usd',coalesce(sum(CASE WHEN state='known' THEN (CASE WHEN kind='embedding' THEN input_tokens*0.1 ELSE input_tokens*1.08+output_tokens*4.32 END)/1000000 ELSE 0 END),0),'unknown_reserved_estimate_usd',coalesce(sum(CASE WHEN state<>'known' THEN (CASE WHEN kind='embedding' THEN reserved_input_tokens*0.1 ELSE reserved_input_tokens*1.08+reserved_output_tokens*4.32 END)/1000000 ELSE 0 END),0)) INTO usage FROM {schema}.rag_attempts WHERE tenant_id=t AND run_id=r.id;
 context:={schema}.authorization_call(token,'finish',CASE WHEN operation='recover' THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Diagnostics authority expired.'; END IF;
 RETURN jsonb_build_object('status','ok','id',r.id,'state',r.state,'version',r.version,'recovered_at',r.recovered_at,'recovery_available',r.state IN ('held','running') AND (j.state<>'running' OR j.lease_until<=clock_timestamp()),'recovery',CASE WHEN operation='recover' THEN 'closed_without_replay' END,'events',events,'usage',usage,'steps',coalesce(b.steps,0),'max_steps',8,'deadline',extract(epoch FROM b.deadline),'authority_expires_at',(SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR lock_not_available THEN RETURN jsonb_build_object('status','conflict');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.rag_owner_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.rag_owner_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='document_sources_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='auth jsonb, refs jsonb, history boolean')
                        AND NOT (f.proname='rag_runtime_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='rag_usage_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='payload jsonb')
                        AND NOT (f.proname='rag_owner_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
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



