-- KNOW-01: private staged ingestion; publication belongs to KNOW-02.
CREATE TABLE {schema}.documents (
 tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),id uuid NOT NULL,owner_id uuid NOT NULL REFERENCES {schema}.principals(id),
 collection_id uuid,title text NOT NULL CHECK(length(title) BETWEEN 1 AND 200),version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 access_revision bigint NOT NULL DEFAULT 1 CHECK(access_revision>0),active_version_id uuid,withdrawn_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,id)
);
CREATE TABLE {schema}.document_versions (
 tenant_id uuid NOT NULL,id uuid NOT NULL,document_id uuid NOT NULL,version_no bigint NOT NULL CHECK(version_no>0),
 file_id uuid NOT NULL,file_digest text NOT NULL,object_version text NOT NULL,size_bytes bigint NOT NULL,mime text NOT NULL,
 job_id uuid NOT NULL,access_revision bigint NOT NULL,parse_state text NOT NULL DEFAULT 'pending' CHECK(parse_state IN ('pending','processing','ready','failed')),
 index_state text NOT NULL DEFAULT 'pending' CHECK(index_state IN ('pending','building','ready','failed')),
 publication_state text NOT NULL DEFAULT 'draft' CHECK(publication_state IN ('draft','published','superseded','withdrawn')),
 parser_ref text CHECK(parser_ref ~ '^sha256:[a-f0-9]{{64}}$' OR parser_ref='fixture:utf8-v1'),index_generation bigint NOT NULL DEFAULT 0 CHECK(index_generation>=0),chunk_count integer CHECK(chunk_count BETWEEN 1 AND 32),
 content_digest text CHECK(content_digest ~ '^[a-f0-9]{{64}}$'),embedding_model text NOT NULL DEFAULT 'cohere.embed-multilingual-v3' CHECK(embedding_model='cohere.embed-multilingual-v3'),
 dimensions integer NOT NULL DEFAULT 1024 CHECK(dimensions=1024),chunk_config text NOT NULL DEFAULT 'utf8-384-nooverlap-v1' CHECK(chunk_config='utf8-384-nooverlap-v1'),
 attempt_embedding_calls integer NOT NULL DEFAULT 0 CHECK(attempt_embedding_calls BETWEEN 0 AND 8),embedding_attempts integer NOT NULL DEFAULT 0 CHECK(embedding_attempts BETWEEN 0 AND 24),error_code text CHECK(error_code IN ('invalid','transient','rate_limited','forbidden')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,document_id,version_no),UNIQUE(tenant_id,id,document_id),
 FOREIGN KEY(tenant_id,document_id) REFERENCES {schema}.documents(tenant_id,id),
 FOREIGN KEY(tenant_id,file_id,file_digest) REFERENCES {schema}.files(tenant_id,id,digest),
 FOREIGN KEY(tenant_id,job_id) REFERENCES {schema}.outbox_jobs(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 CHECK(index_generation=0 OR parser_ref IS NOT NULL),CHECK((index_state='ready')=(parse_state='ready')),CHECK(index_state<>'ready' OR (chunk_count IS NOT NULL AND content_digest IS NOT NULL AND index_generation>0))
);
ALTER TABLE {schema}.documents ADD CONSTRAINT documents_active_version_fk FOREIGN KEY(tenant_id,active_version_id,id)
 REFERENCES {schema}.document_versions(tenant_id,id,document_id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE {schema}.document_acl (
 tenant_id uuid NOT NULL,document_id uuid NOT NULL,principal_id uuid NOT NULL REFERENCES {schema}.principals(id),
 permission text NOT NULL DEFAULT 'read' CHECK(permission='read'),PRIMARY KEY(tenant_id,document_id,principal_id),
 FOREIGN KEY(tenant_id,document_id) REFERENCES {schema}.documents(tenant_id,id)
);
CREATE TABLE {schema}.knowledge_requests (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL REFERENCES {schema}.principals(id),key uuid NOT NULL,operation text NOT NULL CHECK(operation IN ('create','version')),
 request_hash text NOT NULL,document_id uuid NOT NULL,version_id uuid NOT NULL,PRIMARY KEY(tenant_id,actor_id,key),
 FOREIGN KEY(tenant_id,version_id,document_id) REFERENCES {schema}.document_versions(tenant_id,id,document_id)
);
CREATE TABLE {schema}.chunks (
 tenant_id uuid NOT NULL,document_version_id uuid NOT NULL,index_generation bigint NOT NULL CHECK(index_generation>0),chunk_no integer NOT NULL CHECK(chunk_no BETWEEN 0 AND 31),
 page integer NOT NULL CHECK(page BETWEEN 1 AND 100),start_offset integer NOT NULL CHECK(start_offset>=0),end_offset integer NOT NULL,
 text text NOT NULL CHECK(octet_length(text) BETWEEN 1 AND 384),text_digest text NOT NULL CHECK(text_digest ~ '^[a-f0-9]{{64}}$'),
 embedding_model text NOT NULL DEFAULT 'cohere.embed-multilingual-v3' CHECK(embedding_model='cohere.embed-multilingual-v3'),
 PRIMARY KEY(tenant_id,document_version_id,index_generation,chunk_no),
 FOREIGN KEY(tenant_id,document_version_id) REFERENCES {schema}.document_versions(tenant_id,id),CHECK(end_offset>start_offset)
);
DO $$ DECLARE ns text; BEGIN
 SELECT n.nspname INTO ns FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector';
 IF ns IS NULL THEN RAISE EXCEPTION 'pgvector must be installed before KNOW-01 migration.'; END IF;
 EXECUTE format('ALTER TABLE {schema}.chunks ADD COLUMN embedding %I.vector(1024) NOT NULL',ns);
 EXECUTE format('ALTER TABLE {schema}.chunks ADD CONSTRAINT chunks_nonzero CHECK (%I.vector_norm(embedding)>0)',ns);
END $$;
CREATE INDEX chunks_generation ON {schema}.chunks(tenant_id,document_version_id,index_generation);
ALTER TABLE {schema}.documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.documents FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.document_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.document_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.document_acl FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.knowledge_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.knowledge_requests FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.documents TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.document_versions TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.document_acl TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.knowledge_requests TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.chunks TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.documents,{schema}.document_versions,{schema}.document_acl,{schema}.knowledge_requests,{schema}.chunks FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._knowledge_json(d {schema}.documents,v {schema}.document_versions) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('status','ok','document_id',d.id,'document_version',d.version,'version_id',v.id,'version_no',v.version_no,
 'parse_state',v.parse_state,'index_state',v.index_state,'publication_state',v.publication_state,'chunk_count',v.chunk_count,
 'embedding_model',v.embedding_model,'dimensions',v.dimensions,'chunk_config',v.chunk_config,'parser_ref',v.parser_ref,'error_code',v.error_code);
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_json({schema}.documents,{schema}.document_versions) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.knowledge_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;tenant uuid;person uuid;member uuid;sid uuid;
 d {schema}.documents%ROWTYPE;v {schema}.document_versions%ROWTYPE;f {schema}.files%ROWTYPE;j {schema}.outbox_jobs%ROWTYPE;
 r {schema}.knowledge_requests%ROWTYPE;result jsonb;item jsonb;ns text;request_hash text;reader uuid;count_rows integer;min_chunk integer;max_chunk integer;
BEGIN
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
  IF d.id IS NULL OR v.id IS NULL OR NOT {schema}._files_allowed(tenant,person,'knowledge',d.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
 ELSE
  IF NOT payload ?& ARRAY['id','version_id','file_id','key','csrf_hash'] OR (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='create' THEN 7 ELSE 6 END)
   OR (operation='create' AND (NOT payload ?& ARRAY['title','readers'] OR jsonb_typeof(payload->'readers')<>'array' OR jsonb_array_length(payload->'readers')>50 OR length(btrim(payload->>'title')) NOT BETWEEN 1 AND 200))
   OR (operation='version' AND (NOT payload ? 'expected_version' OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR (payload->>'expected_version') !~ '^[1-9][0-9]{{0,17}}$')) THEN RETURN jsonb_build_object('status','invalid'); END IF;
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
   INSERT INTO {schema}.job_tenants(tenant_id) VALUES(tenant) ON CONFLICT DO NOTHING;
   INSERT INTO {schema}.outbox_jobs(tenant_id,id,job_type,business_key,target_id,expected_version,actor_id,membership_id,session_id,access_revision,retry_limit)
    VALUES(tenant,(payload->>'version_id')::uuid,'knowledge.ingest',(payload->>'version_id')::uuid,(payload->>'version_id')::uuid,d.version,person,member,sid,(identity->>'access_revision')::bigint,3);
   INSERT INTO {schema}.document_versions(tenant_id,id,document_id,version_no,file_id,file_digest,object_version,size_bytes,mime,job_id,access_revision)
    VALUES(tenant,(payload->>'version_id')::uuid,d.id,d.version,f.id,f.digest,f.object_version,f.size_bytes,f.mime,(payload->>'version_id')::uuid,d.access_revision) RETURNING * INTO v;
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
REVOKE ALL ON FUNCTION {schema}.knowledge_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.knowledge_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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

