-- KNOW-02: current authority lives on source rows, never in an index/cache snapshot.
CREATE TABLE {schema}.knowledge_events (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL REFERENCES {schema}.principals(id),key uuid NOT NULL,
 operation text NOT NULL CHECK(operation IN ('publish','withdraw','acl')),request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),
 document_id uuid NOT NULL,version_id uuid,result_version bigint NOT NULL,access_revision bigint NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,actor_id,key),
 FOREIGN KEY(tenant_id,document_id) REFERENCES {schema}.documents(tenant_id,id),
 FOREIGN KEY(tenant_id,version_id,document_id) REFERENCES {schema}.document_versions(tenant_id,id,document_id)
);
ALTER TABLE {schema}.knowledge_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.knowledge_events FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.knowledge_events TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.knowledge_events FROM PUBLIC,{access_role};
CREATE UNIQUE INDEX knowledge_one_published ON {schema}.document_versions(tenant_id,document_id) WHERE publication_state='published';
CREATE INDEX chunks_lexical ON {schema}.chunks USING gin(to_tsvector('simple'::regconfig,text));

CREATE FUNCTION {schema}._knowledge_pointer_check() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE t uuid;doc uuid;d {schema}.documents%ROWTYPE;count_published integer;
BEGIN
 IF TG_TABLE_NAME='documents' THEN
  IF TG_OP='DELETE' THEN t:=OLD.tenant_id;doc:=OLD.id;ELSE t:=NEW.tenant_id;doc:=NEW.id;END IF;
 ELSE
  IF TG_OP='DELETE' THEN t:=OLD.tenant_id;doc:=OLD.document_id;ELSE t:=NEW.tenant_id;doc:=NEW.document_id;END IF;
 END IF;
 SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=doc;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT count(*) INTO count_published FROM {schema}.document_versions WHERE tenant_id=t AND document_id=doc AND publication_state='published';
 IF (d.active_version_id IS NULL AND count_published<>0) OR (d.withdrawn_at IS NOT NULL AND d.active_version_id IS NOT NULL)
  OR (d.active_version_id IS NOT NULL AND (count_published<>1 OR NOT EXISTS(SELECT 1 FROM {schema}.document_versions v
   WHERE v.tenant_id=t AND v.document_id=doc AND v.id=d.active_version_id AND v.publication_state='published' AND v.index_state='ready' AND v.parse_state='ready')))
 THEN RAISE EXCEPTION 'Knowledge pointer rejected.' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_pointer_check() FROM PUBLIC,{access_role};
CREATE CONSTRAINT TRIGGER knowledge_document_pointer AFTER INSERT OR UPDATE OR DELETE ON {schema}.documents
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._knowledge_pointer_check();
CREATE CONSTRAINT TRIGGER knowledge_version_pointer AFTER INSERT OR UPDATE OR DELETE ON {schema}.document_versions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {schema}._knowledge_pointer_check();

CREATE FUNCTION {schema}._knowledge_complete(v {schema}.document_versions) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT coalesce(v.parse_state='ready' AND v.index_state='ready' AND v.index_generation>0 AND v.parser_ref IS NOT NULL
  AND v.embedding_model='cohere.embed-multilingual-v3' AND v.dimensions=1024 AND v.chunk_config='utf8-384-nooverlap-v1'
  AND v.content_digest IS NOT NULL AND v.chunk_count BETWEEN 1 AND 32
  AND EXISTS(SELECT 1 FROM {schema}.outbox_jobs j WHERE j.tenant_id=v.tenant_id AND j.id=v.job_id AND j.target_id=v.id
   AND j.job_type='knowledge.ingest' AND j.state='completed' AND j.generation=v.index_generation AND j.expected_version=v.version_no)
  AND (SELECT count(*)=v.chunk_count AND min(chunk_no)=0 AND max(chunk_no)=v.chunk_count-1
       AND sum(octet_length(text))<=12288 AND bool_and(embedding_model=v.embedding_model AND text_digest=encode(sha256(convert_to(text,'UTF8')),'hex'))
    FROM {schema}.chunks WHERE tenant_id=v.tenant_id AND document_version_id=v.id AND index_generation=v.index_generation),false);
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_complete({schema}.document_versions) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._knowledge_readable(t uuid,p uuid,source uuid,history boolean) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM {schema}.documents d JOIN {schema}.document_versions v ON v.tenant_id=d.tenant_id AND v.document_id=d.id
  JOIN {schema}.files f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
  WHERE d.tenant_id=t AND v.id=source AND d.withdrawn_at IS NULL
   AND (d.collection_id IS NULL OR EXISTS(SELECT 1 FROM {schema}.authorization_scopes s WHERE s.tenant_id=t AND s.scope_kind='knowledge_collection' AND s.scope_ref=d.collection_id AND s.active))
   AND (v.publication_state='published' AND d.active_version_id=v.id OR history AND v.publication_state='superseded')
   AND ({schema}._files_allowed(t,p,'knowledge',d.collection_id) OR EXISTS(SELECT 1 FROM {schema}.document_acl a WHERE a.tenant_id=t AND a.document_id=d.id AND a.principal_id=p AND a.permission='read'))
   AND f.purpose='knowledge' AND f.scan_state='ready' AND f.deletion_state IN ('active','retained')
   AND ROW(f.digest,f.object_version,f.size_bytes,f.mime,f.collection_id) IS NOT DISTINCT FROM ROW(v.file_digest,v.object_version,v.size_bytes,v.mime,d.collection_id)
   AND {schema}._knowledge_complete(v));
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_readable(uuid,uuid,uuid,boolean) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._knowledge_source(d {schema}.documents,v {schema}.document_versions) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('document_id',d.id,'title',d.title,'version_id',v.id,'version_no',v.version_no,'publication_state',v.publication_state,
  'access_revision',d.access_revision,'index_generation',v.index_generation,'content_digest',v.content_digest,'file_digest',v.file_digest,
  'embedding_model',v.embedding_model,'chunk_config',v.chunk_config,'parser_ref',v.parser_ref,'format','extracted_chunks',
  'chunks',coalesce((SELECT jsonb_agg(jsonb_build_object('chunk_no',c.chunk_no,'page',c.page,'start',c.start_offset,'end',c.end_offset,'text',c.text,'text_digest',c.text_digest) ORDER BY c.chunk_no)
   FROM {schema}.chunks c WHERE c.tenant_id=v.tenant_id AND c.document_version_id=v.id AND c.index_generation=v.index_generation),'[]'::jsonb));
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_source({schema}.documents,{schema}.document_versions) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._knowledge_lifecycle(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;tenant uuid;person uuid;d {schema}.documents%ROWTYPE;v {schema}.document_versions%ROWTYPE;
 old {schema}.document_versions%ROWTYPE;f {schema}.files%ROWTYPE;e {schema}.knowledge_events%ROWTYPE;
 hash text;reader uuid;result jsonb;item jsonb;items jsonb:='[]'::jsonb;query text;limit_rows integer;deadline numeric;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Knowledge lifecycle rejected.'; END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>16384 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF operation NOT IN ('publish','withdraw','acl','search','search_recheck','source','source_recheck') OR operation IS NULL THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN operation IN ('publish','withdraw','acl') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';tenant:=(identity->>'active_tenant_id')::uuid;person:=(identity->>'principal_id')::uuid;
 IF operation IN ('publish','withdraw','acl') THEN
  IF NOT payload ?& ARRAY['id','expected_version','key','csrf_hash'] OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='acl' THEN 5 ELSE 4 END)
   OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string'
   OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$'
   THEN RETURN jsonb_build_object('status','invalid'); END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('eai:knowledge-key:'||tenant::text||':'||person::text||':'||((payload->>'key')::uuid)::text,0));
  IF operation='publish' THEN
   SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=tenant AND id=(payload->>'id')::uuid;
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=tenant AND id=v.document_id FOR UPDATE;
  ELSE SELECT * INTO d FROM {schema}.documents WHERE tenant_id=tenant AND id=(payload->>'id')::uuid FOR UPDATE;
  END IF;
  IF d.id IS NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
  PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=tenant AND scope_kind='knowledge_collection' AND scope_ref=d.collection_id FOR SHARE;
  IF NOT {schema}._files_allowed(tenant,person,'knowledge',d.collection_id) THEN
   RETURN jsonb_build_object('status',CASE WHEN {schema}._knowledge_readable(tenant,person,CASE WHEN operation='publish' THEN v.id ELSE d.active_version_id END,true) THEN 'forbidden' ELSE 'missing' END);
  END IF;
  hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
  IF EXISTS(SELECT 1 FROM {schema}.knowledge_requests WHERE tenant_id=tenant AND actor_id=person AND key=(payload->>'key')::uuid) THEN RETURN jsonb_build_object('status','conflict'); END IF;
  SELECT * INTO e FROM {schema}.knowledge_events WHERE tenant_id=tenant AND actor_id=person AND key=(payload->>'key')::uuid;
  IF FOUND THEN
   IF e.operation<>operation OR e.request_hash<>hash OR e.document_id<>d.id THEN RETURN jsonb_build_object('status','conflict'); END IF;
  ELSE
   IF d.withdrawn_at IS NOT NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
   IF d.version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
   IF operation='publish' THEN
    SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=tenant AND id=v.id FOR UPDATE;
    SELECT * INTO f FROM {schema}.files WHERE tenant_id=tenant AND id=v.file_id FOR SHARE;
    SELECT * INTO old FROM {schema}.document_versions WHERE tenant_id=tenant AND id=d.active_version_id FOR UPDATE;
    IF v.publication_state<>'draft' OR NOT {schema}._knowledge_complete(v) OR f.id IS NULL OR f.purpose<>'knowledge' OR f.scan_state<>'ready' OR f.deletion_state<>'active'
     OR ROW(f.digest,f.object_version,f.size_bytes,f.mime,f.collection_id) IS DISTINCT FROM ROW(v.file_digest,v.object_version,v.size_bytes,v.mime,d.collection_id)
     OR (old.id IS NOT NULL AND v.version_no<=old.version_no) THEN RETURN jsonb_build_object('status','conflict'); END IF;
    UPDATE {schema}.document_versions SET publication_state='superseded' WHERE tenant_id=tenant AND id=d.active_version_id;
    UPDATE {schema}.document_versions SET publication_state='published' WHERE tenant_id=tenant AND id=v.id;
    UPDATE {schema}.documents SET active_version_id=v.id,version=version+1,access_revision=access_revision+1 WHERE tenant_id=tenant AND id=d.id RETURNING * INTO d;
   ELSIF operation='withdraw' THEN
    UPDATE {schema}.document_versions SET publication_state='withdrawn' WHERE tenant_id=tenant AND document_id=d.id;
    UPDATE {schema}.document_versions SET parse_state='failed',index_state='failed',error_code='forbidden' WHERE tenant_id=tenant AND document_id=d.id AND index_state IN ('pending','building');
    UPDATE {schema}.documents SET active_version_id=NULL,withdrawn_at=clock_timestamp(),version=version+1,access_revision=access_revision+1 WHERE tenant_id=tenant AND id=d.id RETURNING * INTO d;
   ELSE
    IF NOT payload ? 'readers' OR jsonb_typeof(payload->'readers') IS DISTINCT FROM 'array' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF jsonb_array_length(payload->'readers')>50 THEN RETURN jsonb_build_object('status','invalid'); END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(payload->'readers') LOOP
     IF jsonb_typeof(item) IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
     reader:=(item#>>'{{}}')::uuid;
     IF NOT EXISTS(SELECT 1 FROM {schema}.memberships WHERE tenant_id=tenant AND principal_id=reader AND active) THEN RETURN jsonb_build_object('status','invalid'); END IF;
    END LOOP;
    DELETE FROM {schema}.document_acl WHERE tenant_id=tenant AND document_id=d.id;
    INSERT INTO {schema}.document_acl(tenant_id,document_id,principal_id) SELECT tenant,d.id,value::uuid FROM jsonb_array_elements_text(payload->'readers') ON CONFLICT DO NOTHING;
    UPDATE {schema}.document_versions SET parse_state='failed',index_state='failed',error_code='forbidden' WHERE tenant_id=tenant AND document_id=d.id AND index_state IN ('pending','building');
    UPDATE {schema}.documents SET version=version+1,access_revision=access_revision+1 WHERE tenant_id=tenant AND id=d.id RETURNING * INTO d;
   END IF;
   INSERT INTO {schema}.knowledge_events(tenant_id,actor_id,key,operation,request_hash,document_id,version_id,result_version,access_revision)
    VALUES(tenant,person,(payload->>'key')::uuid,operation,hash,d.id,CASE WHEN operation='publish' THEN v.id ELSE NULL END,d.version,d.access_revision) RETURNING * INTO e;
  END IF;
  result:=jsonb_build_object('document_id',d.id,'document_version',d.version,'access_revision',d.access_revision,'active_version_id',d.active_version_id,
    'withdrawn',d.withdrawn_at IS NOT NULL,'operation',e.operation,'operation_version_id',e.version_id,'result_version',e.result_version,'result_access_revision',e.access_revision);
 ELSIF operation IN ('source','source_recheck') THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='source' THEN 1 ELSE 4 END) OR NOT payload ? 'version_id'
   OR jsonb_typeof(payload->'version_id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=tenant AND id=(payload->>'version_id')::uuid;
  SELECT * INTO d FROM {schema}.documents WHERE tenant_id=tenant AND id=v.document_id FOR SHARE;
  SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=tenant AND id=(payload->>'version_id')::uuid FOR SHARE;
  PERFORM id FROM {schema}.files WHERE tenant_id=tenant AND id=v.file_id FOR SHARE;
  PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=tenant AND scope_kind='knowledge_collection' AND scope_ref=d.collection_id FOR SHARE;
  IF d.id IS NULL OR NOT {schema}._knowledge_readable(tenant,person,v.id,true) THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF operation='source_recheck' THEN
   IF NOT payload ?& ARRAY['access_revision','index_generation','content_digest'] OR jsonb_typeof(payload->'access_revision') IS DISTINCT FROM 'number'
    OR jsonb_typeof(payload->'index_generation') IS DISTINCT FROM 'number' OR jsonb_typeof(payload->'content_digest') IS DISTINCT FROM 'string'
    OR ROW(v.index_generation,v.content_digest,d.access_revision) IS DISTINCT FROM ROW((payload->>'index_generation')::bigint,payload->>'content_digest',(payload->>'access_revision')::bigint)
    THEN RETURN jsonb_build_object('status','missing'); END IF;
  END IF;
  result:={schema}._knowledge_source(d,v);
 ELSE
  IF operation='search' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['query','language','limit']
    OR jsonb_typeof(payload->'query') IS DISTINCT FROM 'string' OR length(btrim(payload->>'query')) NOT BETWEEN 1 AND 200 OR octet_length(payload->>'query')>800 OR payload->>'query' ~ '[[:cntrl:]]'
    OR jsonb_typeof(payload->'language') IS DISTINCT FROM 'string' OR payload->>'language' NOT IN ('id','en')
    OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^(10|[1-9])$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
   query:=payload->>'query';limit_rows:=(payload->>'limit')::integer;
   SELECT coalesce(jsonb_agg(hit ORDER BY score DESC,doc,chunk_no),'[]'::jsonb) INTO items FROM (
    SELECT jsonb_build_object('document_id',sd.id,'title',sd.title,'version_id',sv.id,'version_no',sv.version_no,'access_revision',sd.access_revision,
     'index_generation',sv.index_generation,'chunk_no',sc.chunk_no,'page',sc.page,'start',sc.start_offset,'end',sc.end_offset,'text',sc.text,'text_digest',sc.text_digest,
     'content_digest',sv.content_digest,'publication_state',sv.publication_state) AS hit,
     ts_rank_cd(to_tsvector('simple'::regconfig,sc.text),plainto_tsquery('simple'::regconfig,query)) AS score,sd.id AS doc,sc.chunk_no
    FROM {schema}.documents sd JOIN {schema}.document_versions sv ON sv.tenant_id=sd.tenant_id AND sv.id=sd.active_version_id
    JOIN {schema}.chunks sc ON sc.tenant_id=sv.tenant_id AND sc.document_version_id=sv.id AND sc.index_generation=sv.index_generation
    WHERE sd.tenant_id=tenant AND {schema}._knowledge_readable(tenant,person,sv.id,false)
     AND to_tsvector('simple'::regconfig,sc.text) @@ plainto_tsquery('simple'::regconfig,query)
    ORDER BY score DESC,sd.id,sc.chunk_no LIMIT limit_rows) hits;
  ELSE
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 OR NOT payload ? 'items' OR jsonb_typeof(payload->'items') IS DISTINCT FROM 'array' THEN RETURN jsonb_build_object('status','invalid'); END IF;
   IF jsonb_array_length(payload->'items')>10 THEN RETURN jsonb_build_object('status','invalid'); END IF;
   PERFORM d0.id FROM {schema}.documents d0 WHERE d0.tenant_id=tenant AND d0.id IN
    (SELECT v0.document_id FROM {schema}.document_versions v0 JOIN jsonb_array_elements(payload->'items') x ON v0.id=(x.value->>'version_id')::uuid WHERE v0.tenant_id=tenant) ORDER BY d0.id FOR SHARE;
   PERFORM v0.id FROM {schema}.document_versions v0 WHERE v0.tenant_id=tenant AND v0.id IN
    (SELECT (x.value->>'version_id')::uuid FROM jsonb_array_elements(payload->'items') x) ORDER BY v0.id FOR SHARE;
   PERFORM f0.id FROM {schema}.files f0 WHERE f0.tenant_id=tenant AND f0.id IN
    (SELECT v0.file_id FROM {schema}.document_versions v0 JOIN jsonb_array_elements(payload->'items') x ON v0.id=(x.value->>'version_id')::uuid WHERE v0.tenant_id=tenant) ORDER BY f0.id FOR SHARE;
   PERFORM s0.scope_ref FROM {schema}.authorization_scopes s0 WHERE s0.tenant_id=tenant AND s0.scope_kind='knowledge_collection' AND s0.scope_ref IN
    (SELECT d0.collection_id FROM {schema}.documents d0 JOIN {schema}.document_versions v0 ON v0.tenant_id=d0.tenant_id AND v0.document_id=d0.id
     JOIN jsonb_array_elements(payload->'items') x ON v0.id=(x.value->>'version_id')::uuid WHERE d0.tenant_id=tenant) ORDER BY s0.scope_ref FOR SHARE;
   FOR item IN SELECT value FROM jsonb_array_elements(payload->'items') LOOP
    IF jsonb_typeof(item) IS DISTINCT FROM 'object' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(item))<>5 OR NOT item ?& ARRAY['version_id','chunk_no','index_generation','access_revision','text_digest']
     OR jsonb_typeof(item->'version_id') IS DISTINCT FROM 'string' OR jsonb_typeof(item->'chunk_no') IS DISTINCT FROM 'number'
     OR jsonb_typeof(item->'index_generation') IS DISTINCT FROM 'number' OR jsonb_typeof(item->'access_revision') IS DISTINCT FROM 'number'
     OR jsonb_typeof(item->'text_digest') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=tenant AND id=(item->>'version_id')::uuid;
    SELECT * INTO d FROM {schema}.documents WHERE tenant_id=tenant AND id=v.document_id;
    IF d.id IS NOT NULL AND {schema}._knowledge_readable(tenant,person,v.id,false) AND d.access_revision=(item->>'access_revision')::bigint AND v.index_generation=(item->>'index_generation')::bigint THEN
     SELECT jsonb_build_object('document_id',d.id,'title',d.title,'version_id',v.id,'version_no',v.version_no,'access_revision',d.access_revision,
      'index_generation',v.index_generation,'chunk_no',c.chunk_no,'page',c.page,'start',c.start_offset,'end',c.end_offset,'text',c.text,'text_digest',c.text_digest,
      'content_digest',v.content_digest,'publication_state',v.publication_state) INTO result
      FROM {schema}.chunks c WHERE c.tenant_id=tenant AND c.document_version_id=v.id AND c.index_generation=v.index_generation AND c.chunk_no=(item->>'chunk_no')::integer AND c.text_digest=item->>'text_digest';
     IF result IS NOT NULL THEN items:=items||jsonb_build_array(result); END IF;
    END IF;
   END LOOP;
  END IF;
  result:=jsonb_build_object('items',items,'search_mode','lexical_simple_v1');
 END IF;
 context:={schema}.authorization_call(token,'finish',CASE WHEN operation IN ('publish','withdraw','acl') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Knowledge authority expired.' USING ERRCODE='40001'; END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 RETURN result||jsonb_build_object('status','ok','authority_expires_at',deadline);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation OR foreign_key_violation OR invalid_parameter_value THEN
 RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_lifecycle(text,text,jsonb) FROM PUBLIC,{access_role};

CREATE OR REPLACE FUNCTION {schema}.knowledge_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;tenant uuid;person uuid;member uuid;sid uuid;
 d {schema}.documents%ROWTYPE;v {schema}.document_versions%ROWTYPE;f {schema}.files%ROWTYPE;j {schema}.outbox_jobs%ROWTYPE;
 r {schema}.knowledge_requests%ROWTYPE;result jsonb;item jsonb;ns text;request_hash text;reader uuid;count_rows integer;min_chunk integer;max_chunk integer;ordinal bigint;
BEGIN
 IF operation IN ('publish','withdraw','acl','search','search_recheck','source','source_recheck') THEN RETURN {schema}._knowledge_lifecycle(token,operation,payload); END IF;
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Knowledge capability rejected.'; END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>262144 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF operation IN ('begin','reference','reserve_embedding','stage','ready','failed') THEN
  IF NOT payload ?& ARRAY['credential_hash','job_id','generation'] THEN RETURN jsonb_build_object('status','invalid'); END IF;
  result:={schema}.jobs_call('reference',jsonb_build_object('credential_hash',payload->>'credential_hash','id',payload->>'job_id','generation',payload->'generation'));
  IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
  SELECT a.* INTO j FROM {schema}.outbox_jobs a JOIN {schema}.job_callers c ON c.id=a.worker_id AND c.tenant_id=a.tenant_id
   WHERE a.id=(payload->>'job_id')::uuid AND c.credential_hash=payload->>'credential_hash' AND a.job_type='knowledge.ingest';
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
  SELECT a.* INTO d FROM {schema}.documents a JOIN {schema}.document_versions b ON b.tenant_id=a.tenant_id AND b.document_id=a.id
   WHERE b.tenant_id=j.tenant_id AND b.id=j.target_id FOR UPDATE OF a;
  SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=j.tenant_id AND id=j.target_id FOR UPDATE;
  SELECT * INTO f FROM {schema}.files WHERE tenant_id=v.tenant_id AND id=v.file_id FOR SHARE;
  IF v.id IS NULL OR v.job_id<>j.id OR v.version_no<>j.expected_version OR v.access_revision<>d.access_revision OR d.withdrawn_at IS NOT NULL
   OR v.publication_state<>'draft' OR f.id IS NULL OR f.purpose<>'knowledge' OR f.scan_state<>'ready' OR f.deletion_state<>'active'
   OR ROW(f.digest,f.object_version,f.size_bytes,f.mime,f.collection_id) IS DISTINCT FROM ROW(v.file_digest,v.object_version,v.size_bytes,v.mime,d.collection_id)
   OR NOT {schema}._files_allowed(j.tenant_id,j.actor_id,'knowledge',d.collection_id) THEN RETURN jsonb_build_object('status','held'); END IF;
  IF operation='begin' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR jsonb_typeof(payload->'parser_ref') IS DISTINCT FROM 'string' OR NOT (payload->>'parser_ref' ~ '^sha256:[a-f0-9]{{64}}$' OR payload->>'parser_ref'='fixture:utf8-v1') OR v.index_state='ready' OR v.index_generation>=j.generation THEN RETURN jsonb_build_object('status','conflict'); END IF;
   DELETE FROM {schema}.chunks WHERE tenant_id=v.tenant_id AND document_version_id=v.id;
   UPDATE {schema}.document_versions SET parse_state='processing',index_state='building',index_generation=j.generation,parser_ref=payload->>'parser_ref',attempt_embedding_calls=0,chunk_count=NULL,content_digest=NULL,error_code=NULL
    WHERE tenant_id=v.tenant_id AND id=v.id RETURNING * INTO v;
  ELSE
   IF v.index_generation<>j.generation OR v.index_state<>'building' THEN RETURN jsonb_build_object('status','stale'); END IF;
   IF operation='reserve_embedding' THEN
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR v.embedding_attempts>=24 OR v.attempt_embedding_calls>=8 THEN RETURN jsonb_build_object('status','capacity'); END IF;
    UPDATE {schema}.document_versions SET embedding_attempts=embedding_attempts+1,attempt_embedding_calls=attempt_embedding_calls+1 WHERE tenant_id=v.tenant_id AND id=v.id;
   ELSIF operation='stage' THEN
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR jsonb_typeof(payload->'chunks')<>'array' OR jsonb_array_length(payload->'chunks') NOT BETWEEN 1 AND 4 THEN RETURN jsonb_build_object('status','invalid'); END IF;
    SELECT n.nspname INTO ns FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector';
    FOR item IN SELECT value FROM jsonb_array_elements(payload->'chunks') LOOP
     IF (SELECT count(*) FROM jsonb_object_keys(item))<>8 OR NOT item ?& ARRAY['number','page','start','end','text','digest','vector','model']
      OR item->>'model'<>'cohere.embed-multilingual-v3' OR item->>'digest' IS DISTINCT FROM encode(sha256(convert_to(item->>'text','UTF8')),'hex')
      OR (item->>'end')::integer-(item->>'start')::integer<>char_length(item->>'text') OR jsonb_array_length(item->'vector')<>1024
      THEN RAISE EXCEPTION 'Invalid chunk batch.' USING ERRCODE='23514'; END IF;
     EXECUTE format('INSERT INTO {schema}.chunks(tenant_id,document_version_id,index_generation,chunk_no,page,start_offset,end_offset,text,text_digest,embedding) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::%I.vector)',ns)
      USING v.tenant_id,v.id,j.generation,(item->>'number')::integer,(item->>'page')::integer,(item->>'start')::integer,(item->>'end')::integer,item->>'text',item->>'digest',(item->'vector')::text;
    END LOOP;
   ELSIF operation='ready' THEN
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['chunk_count','content_digest'] OR jsonb_typeof(payload->'chunk_count') IS DISTINCT FROM 'number' OR jsonb_typeof(payload->'content_digest') IS DISTINCT FROM 'string' OR (payload->>'chunk_count') !~ '^[1-9][0-9]?$' OR (payload->>'chunk_count')::integer>32
     OR payload->>'content_digest' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    SELECT count(*),min(chunk_no),max(chunk_no) INTO count_rows,min_chunk,max_chunk FROM {schema}.chunks WHERE tenant_id=v.tenant_id AND document_version_id=v.id AND index_generation=j.generation;
    IF count_rows<>(payload->>'chunk_count')::integer OR min_chunk<>0 OR max_chunk<>count_rows-1
     THEN RETURN jsonb_build_object('status','conflict'); END IF;
    result:={schema}.jobs_call('complete',jsonb_build_object('credential_hash',payload->>'credential_hash','id',j.id,'generation',j.generation));
    IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
    UPDATE {schema}.document_versions SET parse_state='ready',index_state='ready',chunk_count=count_rows,content_digest=payload->>'content_digest' WHERE tenant_id=v.tenant_id AND id=v.id RETURNING * INTO v;
    RETURN {schema}._knowledge_json(d,v);
   ELSIF operation='failed' THEN
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR payload->>'error_code' NOT IN ('invalid','transient','rate_limited','forbidden') THEN RETURN jsonb_build_object('status','invalid'); END IF;
    result:={schema}.jobs_call('fail',jsonb_build_object('credential_hash',payload->>'credential_hash','id',j.id,'generation',j.generation,'error_code',payload->>'error_code'));
    IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
    UPDATE {schema}.document_versions SET parse_state='failed',index_state='failed',error_code=payload->>'error_code' WHERE tenant_id=v.tenant_id AND id=v.id RETURNING * INTO v;
    RETURN {schema}._knowledge_json(d,v);
   ELSIF operation<>'reference' OR (SELECT count(*) FROM jsonb_object_keys(payload))<>3 THEN RETURN jsonb_build_object('status','invalid'); END IF;
  END IF;
  result:={schema}.jobs_call('reference',jsonb_build_object('credential_hash',payload->>'credential_hash','id',j.id,'generation',j.generation));
  IF result->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Knowledge lease expired.' USING ERRCODE='40001'; END IF;
  RETURN jsonb_build_object('status','ok','id',v.id,'file_id',f.id,'object_key',f.object_key,'object_version',v.object_version,'size_bytes',v.size_bytes,'digest',v.file_digest,'mime',v.mime,'generation',v.index_generation,'authority_expires_at',
   (SELECT extract(epoch FROM least(j.lease_until,j.attempt_deadline,c.expires_at,s.expires_at,s.last_seen_at+interval '1800 seconds'))
    FROM {schema}.job_callers c JOIN {schema}.sessions s ON s.id=j.session_id WHERE c.id=j.worker_id));
 END IF;
 IF operation NOT IN ('create','version','status') OR octet_length(payload::text)>16384 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN operation='status' THEN '{{}}'::jsonb ELSE jsonb_build_object('csrf_hash',payload->>'csrf_hash') END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';tenant:=(identity->>'active_tenant_id')::uuid;person:=(identity->>'principal_id')::uuid;member:=(identity->>'active_membership_id')::uuid;
 SELECT id INTO sid FROM {schema}.sessions WHERE token_hash=token;
 IF operation='status' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>2 OR NOT payload ?& ARRAY['id','version_id'] THEN RETURN jsonb_build_object('status','invalid'); END IF;
  SELECT * INTO d FROM {schema}.documents WHERE tenant_id=tenant AND id=(payload->>'id')::uuid;
  SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=tenant AND document_id=d.id AND id=(payload->>'version_id')::uuid;
  IF d.id IS NULL OR v.id IS NULL OR d.withdrawn_at IS NOT NULL OR NOT {schema}._files_allowed(tenant,person,'knowledge',d.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
 ELSE
  IF NOT payload ?& ARRAY['id','version_id','file_id','key','csrf_hash'] OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='create' THEN 7 ELSE 6 END)
   OR (operation='create' AND (NOT payload ?& ARRAY['title','readers'] OR jsonb_typeof(payload->'readers')<>'array' OR jsonb_array_length(payload->'readers')>50 OR length(btrim(payload->>'title')) NOT BETWEEN 1 AND 200))
   OR (operation='version' AND (NOT payload ? 'expected_version' OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR (payload->>'expected_version') !~ '^[1-9][0-9]{{0,17}}$')) THEN RETURN jsonb_build_object('status','invalid'); END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('eai:knowledge-key:'||tenant::text||':'||person::text||':'||((payload->>'key')::uuid)::text,0));
  IF operation='create' THEN
   SELECT * INTO f FROM {schema}.files WHERE tenant_id=tenant AND id=(payload->>'file_id')::uuid AND owner_principal_id=person FOR SHARE;
   IF f.id IS NULL OR f.purpose<>'knowledge' OR NOT {schema}._files_allowed(tenant,person,'knowledge',f.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
  ELSE
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=tenant AND id=(payload->>'id')::uuid FOR UPDATE;
   IF d.id IS NULL OR d.withdrawn_at IS NOT NULL OR NOT {schema}._files_allowed(tenant,person,'knowledge',d.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
   SELECT * INTO f FROM {schema}.files WHERE tenant_id=tenant AND id=(payload->>'file_id')::uuid AND owner_principal_id=person AND collection_id IS NOT DISTINCT FROM d.collection_id FOR SHARE;
  END IF;
  IF f.id IS NULL OR f.purpose<>'knowledge' OR f.scan_state<>'ready' OR f.deletion_state<>'active' OR f.object_version IS NULL THEN RETURN jsonb_build_object('status','conflict'); END IF;
  PERFORM singleton FROM {schema}.job_scheduler FOR UPDATE;
  context:={schema}.authorization_call(token,'context',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
  IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
  IF NOT {schema}._files_allowed(tenant,person,'knowledge',f.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF EXISTS(SELECT 1 FROM {schema}.knowledge_events WHERE tenant_id=tenant AND actor_id=person AND key=(payload->>'key')::uuid) THEN RETURN jsonb_build_object('status','conflict'); END IF;
  request_hash:=encode(sha256(convert_to((CASE WHEN operation='create' THEN payload-'id'-'version_id'-'csrf_hash' ELSE payload-'version_id'-'csrf_hash' END)::text,'UTF8')),'hex');
  SELECT * INTO r FROM {schema}.knowledge_requests WHERE tenant_id=tenant AND actor_id=person AND key=(payload->>'key')::uuid;
  IF FOUND THEN
   IF r.operation<>operation OR r.request_hash<>request_hash THEN RETURN jsonb_build_object('status','conflict'); END IF;
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=tenant AND id=r.document_id;
   SELECT * INTO v FROM {schema}.document_versions WHERE tenant_id=tenant AND id=r.version_id;
  ELSE
   IF (SELECT count(*) FROM {schema}.document_versions WHERE tenant_id=tenant)>=1000 OR (SELECT count(*) FROM {schema}.document_versions)>=10000
    OR (SELECT count(*) FROM {schema}.outbox_jobs WHERE tenant_id=tenant AND state IN ('queued','retry','running'))>=100
    OR (SELECT count(*) FROM {schema}.outbox_jobs WHERE state IN ('queued','retry','running'))>=1000 THEN RETURN jsonb_build_object('status','capacity'); END IF;
   IF operation='create' THEN
    INSERT INTO {schema}.documents(tenant_id,id,owner_id,collection_id,title) VALUES(tenant,(payload->>'id')::uuid,person,f.collection_id,btrim(payload->>'title')) RETURNING * INTO d;
    INSERT INTO {schema}.document_acl(tenant_id,document_id,principal_id) VALUES(tenant,d.id,person);
    FOR reader IN SELECT value::text::uuid FROM jsonb_array_elements_text(payload->'readers') LOOP
     IF NOT EXISTS(SELECT 1 FROM {schema}.memberships WHERE tenant_id=tenant AND principal_id=reader AND active) THEN RAISE EXCEPTION 'Invalid knowledge reader.' USING ERRCODE='23514'; END IF;
     INSERT INTO {schema}.document_acl(tenant_id,document_id,principal_id) VALUES(tenant,d.id,reader) ON CONFLICT DO NOTHING;
    END LOOP;
   ELSE
    IF d.version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
    UPDATE {schema}.documents SET version=version+1 WHERE tenant_id=tenant AND id=d.id RETURNING * INTO d;
   END IF;
   SELECT coalesce(max(version_no),0)+1 INTO ordinal FROM {schema}.document_versions WHERE tenant_id=tenant AND document_id=d.id;
   INSERT INTO {schema}.job_tenants(tenant_id) VALUES(tenant) ON CONFLICT DO NOTHING;
   INSERT INTO {schema}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision,retry_limit)
    VALUES(tenant,(payload->>'version_id')::uuid,'knowledge.ingest',(payload->>'version_id')::uuid,(payload->>'version_id')::uuid,ordinal,person,member,sid,(identity->>'access_revision')::bigint,3);
   INSERT INTO {schema}.document_versions(tenant_id,id,document_id,version_no,file_id,file_digest,object_version,size_bytes,mime,job_id,access_revision)
    VALUES(tenant,(payload->>'version_id')::uuid,d.id,ordinal,f.id,f.digest,f.object_version,f.size_bytes,f.mime,(payload->>'version_id')::uuid,d.access_revision) RETURNING * INTO v;
   INSERT INTO {schema}.knowledge_requests(tenant_id,actor_id,key,operation,request_hash,document_id,version_id) VALUES(tenant,person,(payload->>'key')::uuid,operation,request_hash,d.id,v.id);
  END IF;
 END IF;
 context:={schema}.authorization_call(token,'finish',CASE WHEN operation='status' THEN '{{}}'::jsonb ELSE jsonb_build_object('csrf_hash',payload->>'csrf_hash') END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Knowledge authority expired.' USING ERRCODE='40001'; END IF;
 RETURN {schema}._knowledge_json(d,v)||jsonb_build_object('authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation OR foreign_key_violation THEN
 RETURN jsonb_build_object('status','invalid');
END;
$$;

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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


CREATE OR REPLACE FUNCTION {schema}._knowledge_json(d {schema}.documents,v {schema}.document_versions) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('status','ok','document_id',d.id,'document_version',d.version,'access_revision',d.access_revision,'active_version_id',d.active_version_id,'withdrawn',d.withdrawn_at IS NOT NULL,'version_id',v.id,'version_no',v.version_no,
 'parse_state',v.parse_state,'index_state',v.index_state,'publication_state',v.publication_state,'chunk_count',v.chunk_count,
 'embedding_model',v.embedding_model,'dimensions',v.dimensions,'chunk_config',v.chunk_config,'parser_ref',v.parser_ref,'error_code',v.error_code);
$$;
