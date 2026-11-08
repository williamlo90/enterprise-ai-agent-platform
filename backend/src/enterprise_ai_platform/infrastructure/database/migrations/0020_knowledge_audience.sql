-- BD-FE-03: document-level dynamic employees or frozen named readers.
ALTER TABLE {schema}.documents ADD COLUMN audience_mode text NOT NULL DEFAULT 'specific_people'
 CHECK(audience_mode IN ('specific_people','all_current_employees'));
ALTER TABLE {schema}.knowledge_requests ADD COLUMN audience_material jsonb NOT NULL DEFAULT '{{}}'::jsonb,
 ADD COLUMN audience_receipt jsonb;
ALTER TABLE {schema}.knowledge_events DROP CONSTRAINT knowledge_events_operation_check;
ALTER TABLE {schema}.knowledge_events ADD CONSTRAINT knowledge_events_operation_check CHECK(operation IN ('publish','withdraw','acl','audience'));
ALTER TABLE {schema}.knowledge_events ADD COLUMN response jsonb;

CREATE TABLE {schema}.knowledge_selections (
 tenant_id uuid NOT NULL,id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,collection_id uuid,
 base_document_id uuid,expected_document_version bigint,
 snapshot_digest text NOT NULL CHECK(snapshot_digest ~ '^[a-f0-9]{{64}}$'),
 items jsonb NOT NULL CHECK(jsonb_typeof(items)='array' AND jsonb_array_length(items)<=1000 AND octet_length(items::text)<=2097152),
 creator_grant_included boolean NOT NULL,retain_existing boolean NOT NULL DEFAULT true,created_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,
 consumed_document_id uuid,consumed_key uuid,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,actor_id,key),
 FOREIGN KEY(tenant_id,actor_id) REFERENCES {schema}.memberships(tenant_id,principal_id),
 FOREIGN KEY(tenant_id,base_document_id) REFERENCES {schema}.documents(tenant_id,id),
 FOREIGN KEY(tenant_id,consumed_document_id) REFERENCES {schema}.documents(tenant_id,id),
 CHECK((base_document_id IS NULL)=(expected_document_version IS NULL)),CHECK(expected_document_version IS NULL OR expected_document_version>0),
 CHECK(expires_at=created_at+interval '900 seconds'),CHECK((consumed_document_id IS NULL)=(consumed_key IS NULL))
);
CREATE INDEX knowledge_selection_expiry ON {schema}.knowledge_selections(expires_at);
CREATE TABLE {schema}.knowledge_selection_requests (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),
 response jsonb NOT NULL CHECK(octet_length(response::text)<=2048),expires_at timestamptz NOT NULL,
 PRIMARY KEY(tenant_id,actor_id,key),FOREIGN KEY(tenant_id,actor_id) REFERENCES {schema}.memberships(tenant_id,principal_id)
);
ALTER TABLE {schema}.knowledge_selections ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.knowledge_selections FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.knowledge_selection_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.knowledge_selection_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.knowledge_selections TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.knowledge_selection_requests TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.knowledge_selections,{schema}.knowledge_selection_requests FROM PUBLIC,{access_role};

CREATE OR REPLACE FUNCTION {schema}._knowledge_readable(t uuid,p uuid,source uuid,history boolean) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM {schema}.documents d JOIN {schema}.document_versions v ON v.tenant_id=d.tenant_id AND v.document_id=d.id
  JOIN {schema}.files f ON f.tenant_id=v.tenant_id AND f.id=v.file_id
  JOIN {schema}.memberships m ON m.tenant_id=t AND m.principal_id=p AND m.active
  JOIN {schema}.principals person ON person.id=p AND person.active
  WHERE d.tenant_id=t AND v.id=source AND d.withdrawn_at IS NULL
   AND (d.collection_id IS NULL OR EXISTS(SELECT 1 FROM {schema}.authorization_scopes s WHERE s.tenant_id=t AND s.scope_kind='knowledge_collection' AND s.scope_ref=d.collection_id AND s.active))
   AND (v.publication_state='published' AND d.active_version_id=v.id OR history AND v.publication_state='superseded')
   AND ({schema}._files_allowed(t,p,'knowledge',d.collection_id)
    OR d.audience_mode='specific_people' AND EXISTS(SELECT 1 FROM {schema}.document_acl a WHERE a.tenant_id=t AND a.document_id=d.id AND a.principal_id=p AND a.permission='read')
    OR d.audience_mode='all_current_employees' AND EXISTS(SELECT 1 FROM {schema}.membership_roles r WHERE r.tenant_id=t AND r.membership_id=m.id AND r.role='employee' AND r.scope_kind='tenant' AND r.scope_ref=t::text))
   AND f.purpose='knowledge' AND f.scan_state='ready' AND f.deletion_state IN ('active','retained')
   AND ROW(f.digest,f.object_version,f.size_bytes,f.mime,f.collection_id) IS NOT DISTINCT FROM ROW(v.file_digest,v.object_version,v.size_bytes,v.mime,d.collection_id)
   AND {schema}._knowledge_complete(v));
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_readable(uuid,uuid,uuid,boolean) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._knowledge_selection_receipt(s {schema}.knowledge_selections) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('selection_id',s.id,'selected_count',jsonb_array_length(s.items),'snapshot_digest',s.snapshot_digest,
 'expires_at',extract(epoch FROM s.expires_at),'creator_grant_included',s.creator_grant_included,
 'base_document_id',s.base_document_id,'expected_document_version',s.expected_document_version,'collection_id',s.collection_id,'retain_existing',s.retain_existing);
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_selection_receipt({schema}.knowledge_selections) FROM PUBLIC,{access_role};

-- Caller must already hold its document (if any) and collection locks. Do not
-- wait in reverse identity/role lock order while acquiring reader facts.
CREATE FUNCTION {schema}._knowledge_selection_live(t uuid,p uuid,selection uuid,collection uuid,base_document uuid,expected bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE s {schema}.knowledge_selections%ROWTYPE;
BEGIN
 SELECT * INTO s FROM {schema}.knowledge_selections WHERE tenant_id=t AND id=selection AND actor_id=p FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
 IF s.collection_id IS DISTINCT FROM collection OR s.base_document_id IS DISTINCT FROM base_document
  OR s.expected_document_version IS DISTINCT FROM expected THEN RETURN jsonb_build_object('status','conflict'); END IF;
 IF s.expires_at<=clock_timestamp() OR s.consumed_key IS NOT NULL THEN RETURN jsonb_build_object('status','conflict'); END IF;
 PERFORM person.id FROM {schema}.principals person JOIN jsonb_array_elements(s.items) item ON person.id=(item->>'principal_id')::uuid ORDER BY person.id FOR SHARE OF person NOWAIT;
 PERFORM m.id FROM {schema}.memberships m JOIN jsonb_array_elements(s.items) item ON m.principal_id=(item->>'principal_id')::uuid
  WHERE m.tenant_id=t ORDER BY m.id FOR SHARE OF m NOWAIT;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(s.items) item
  LEFT JOIN {schema}.private_organization_members m ON m.tenant_id=t AND m.principal_id=(item->>'principal_id')::uuid
  LEFT JOIN {schema}.principals person ON person.id=(item->>'principal_id')::uuid
  WHERE person.id IS NULL OR m.membership_id IS DISTINCT FROM (item->>'membership_id')::uuid
   OR coalesce(m.membership_active,false) IS DISTINCT FROM (item->>'membership_active')::boolean
   OR person.active IS DISTINCT FROM (item->>'principal_active')::boolean
   OR ((NOT coalesce(m.membership_active,false) OR NOT person.active) AND
    (base_document IS NULL OR NOT EXISTS(SELECT 1 FROM {schema}.document_acl a WHERE a.tenant_id=t AND a.document_id=base_document AND a.principal_id=person.id))))
  THEN RETURN jsonb_build_object('status','conflict'); END IF;
 RETURN jsonb_build_object('status','ok','items',s.items,'expires_at',extract(epoch FROM s.expires_at));
EXCEPTION WHEN lock_not_available THEN RETURN jsonb_build_object('status','conflict');
END;
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_selection_live(uuid,uuid,uuid,uuid,uuid,bigint) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._knowledge_access(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;d {schema}.documents%ROWTYPE;s {schema}.knowledge_selections%ROWTYPE;
 prior {schema}.knowledge_selection_requests%ROWTYPE;e {schema}.knowledge_events%ROWTYPE;
 result jsonb;items jsonb;checked jsonb;request_hash text;collection uuid;base_id uuid;limit_rows integer;cursor_id uuid;
 expected bigint;selected_ids uuid[];include_ids uuid[];exclude_ids uuid[];moment timestamptz;field_name text;
BEGIN
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>65536 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF operation NOT IN ('audience','audience_replace','selection_create','selection') THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN operation IN ('audience_replace','selection_create') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 IF operation IN ('audience','audience_replace') THEN
  IF NOT payload ? 'id' OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF operation='audience_replace' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR NOT payload ?& ARRAY['id','audience_mode','selection_id','expected_version','key','csrf_hash']
    OR jsonb_typeof(payload->'audience_mode') IS DISTINCT FROM 'string' OR payload->>'audience_mode' NOT IN ('specific_people','all_current_employees')
    OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number'
    OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$'
    OR (payload->>'audience_mode'='specific_people' AND jsonb_typeof(payload->'selection_id') IS DISTINCT FROM 'string')
    OR (payload->>'audience_mode'='all_current_employees' AND payload->'selection_id'<>'null'::jsonb) THEN RETURN jsonb_build_object('status','invalid'); END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended('eai:knowledge-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=(payload->>'id')::uuid FOR UPDATE;
  ELSE
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['after','limit']
    OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^([1-9]|[1-4][0-9]|50)$'
    OR (payload->'after'<>'null'::jsonb AND jsonb_typeof(payload->'after') IS DISTINCT FROM 'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=(payload->>'id')::uuid FOR SHARE;
  END IF;
  IF d.id IS NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF d.collection_id IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=d.collection_id FOR SHARE; END IF;
  IF NOT {schema}._files_allowed(t,p,'knowledge',d.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF operation='audience_replace' THEN
   request_hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
   IF EXISTS(SELECT 1 FROM {schema}.knowledge_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid) THEN RETURN jsonb_build_object('status','conflict'); END IF;
   SELECT * INTO e FROM {schema}.knowledge_events WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
   IF e.key IS NOT NULL THEN
    IF e.operation<>'audience' OR e.request_hash<>request_hash THEN RETURN jsonb_build_object('status','conflict'); END IF;
    result:=e.response;
   ELSE
    IF d.withdrawn_at IS NOT NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
    IF d.version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
    IF payload->>'audience_mode'='specific_people' THEN
     checked:={schema}._knowledge_selection_live(t,p,(payload->>'selection_id')::uuid,d.collection_id,d.id,d.version);
     IF checked->>'status' IS DISTINCT FROM 'ok' THEN RETURN checked; END IF;
    END IF;
    DELETE FROM {schema}.document_acl WHERE tenant_id=t AND document_id=d.id;
    IF payload->>'audience_mode'='specific_people' THEN
     INSERT INTO {schema}.document_acl(tenant_id,document_id,principal_id) SELECT t,d.id,(value->>'principal_id')::uuid FROM jsonb_array_elements(checked->'items');
    END IF;
    UPDATE {schema}.document_versions SET parse_state='failed',index_state='failed',error_code='forbidden' WHERE tenant_id=t AND document_id=d.id AND index_state IN ('pending','building');
    UPDATE {schema}.documents SET audience_mode=payload->>'audience_mode',version=version+1,access_revision=access_revision+1 WHERE tenant_id=t AND id=d.id RETURNING * INTO d;
    IF payload->>'audience_mode'='specific_people' THEN
     IF extract(epoch FROM clock_timestamp())>=(checked->>'expires_at')::numeric THEN RAISE EXCEPTION 'Selection expired.' USING ERRCODE='P0002'; END IF;
     UPDATE {schema}.knowledge_selections SET consumed_document_id=d.id,consumed_key=(payload->>'key')::uuid WHERE tenant_id=t AND id=(payload->>'selection_id')::uuid;
    END IF;
    result:=jsonb_build_object('document_id',d.id,'document_version',d.version,'access_revision',d.access_revision,'audience_mode',d.audience_mode,
     'explicit_readers_count',(SELECT count(*) FROM {schema}.document_acl WHERE tenant_id=t AND document_id=d.id),'selection_id',payload->'selection_id');
    INSERT INTO {schema}.knowledge_events(tenant_id,actor_id,key,operation,request_hash,document_id,result_version,access_revision,response)
     VALUES(t,p,(payload->>'key')::uuid,'audience',request_hash,d.id,d.version,d.access_revision,result);
   END IF;
  ELSE
   IF d.withdrawn_at IS NOT NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
   limit_rows:=(payload->>'limit')::integer;
   IF payload->>'after' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.document_acl a WHERE a.tenant_id=t AND a.document_id=d.id AND a.principal_id=(payload->>'after')::uuid) THEN RETURN jsonb_build_object('status','missing'); END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY principal_id),'[]'::jsonb) INTO items FROM (
    SELECT jsonb_build_object('principal_id',a.principal_id,'membership_id',m.membership_id,'display_name',coalesce(m.display_name,CASE WHEN {schema}._organization_label(btrim(person.display_name),200,800) THEN btrim(person.display_name) END),
     'membership_active',coalesce(m.membership_active,false),'principal_active',person.active,'eligible',coalesce(m.membership_active,false) AND person.active) value,a.principal_id
    FROM {schema}.document_acl a JOIN {schema}.principals person ON person.id=a.principal_id
    LEFT JOIN {schema}.private_organization_members m ON m.tenant_id=t AND m.principal_id=a.principal_id
    WHERE a.tenant_id=t AND a.document_id=d.id AND (payload->>'after' IS NULL OR a.principal_id>(payload->>'after')::uuid)
    ORDER BY a.principal_id LIMIT limit_rows+1) q;
   IF jsonb_array_length(items)>limit_rows THEN items:=items-limit_rows;cursor_id:=(items->(limit_rows-1)->>'principal_id')::uuid; END IF;
   result:=jsonb_build_object('document_id',d.id,'document_version',d.version,'access_revision',d.access_revision,'audience_mode',d.audience_mode,
    'explicit_readers_count',(SELECT count(*) FROM {schema}.document_acl WHERE tenant_id=t AND document_id=d.id),'readers',items,'next_after',cursor_id,
    'other_authorities',jsonb_build_array('knowledge_manager'),'dynamic_employee_role',d.audience_mode='all_current_employees');
  END IF;
 ELSIF operation='selection' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['id','after','limit']
   OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number'
   OR payload->>'limit' !~ '^([1-9]|[1-4][0-9]|50)$' OR (payload->'after'<>'null'::jsonb AND jsonb_typeof(payload->'after') IS DISTINCT FROM 'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
  SELECT * INTO s FROM {schema}.knowledge_selections WHERE tenant_id=t AND actor_id=p AND id=(payload->>'id')::uuid;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
  -- Document before snapshot: same order as apply, even for read-only preview.
  IF s.base_document_id IS NOT NULL THEN
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=s.base_document_id FOR SHARE;
   IF NOT FOUND OR d.withdrawn_at IS NOT NULL THEN RETURN jsonb_build_object('status','missing'); END IF;
  END IF;
  IF s.collection_id IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=s.collection_id FOR SHARE; END IF;
  IF NOT {schema}._files_allowed(t,p,'knowledge',s.collection_id) THEN RETURN jsonb_build_object('status','missing'); END IF;
  SELECT * INTO s FROM {schema}.knowledge_selections WHERE tenant_id=t AND actor_id=p AND id=s.id FOR SHARE;
  IF NOT FOUND OR s.expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('status','conflict'); END IF;
  limit_rows:=(payload->>'limit')::integer;
  IF payload->>'after' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(s.items) item WHERE item->>'principal_id'=payload->>'after') THEN RETURN jsonb_build_object('status','missing'); END IF;
  SELECT coalesce(jsonb_agg(value ORDER BY value->>'principal_id'),'[]'::jsonb) INTO items FROM (
   SELECT value FROM jsonb_array_elements(s.items) WHERE payload->>'after' IS NULL OR (value->>'principal_id')::uuid>(payload->>'after')::uuid ORDER BY value->>'principal_id' LIMIT limit_rows+1) q;
  IF jsonb_array_length(items)>limit_rows THEN items:=items-limit_rows;cursor_id:=(items->(limit_rows-1)->>'principal_id')::uuid; END IF;
  result:={schema}._knowledge_selection_receipt(s)||jsonb_build_object('items',items,'next_after',cursor_id,'consumed',s.consumed_key IS NOT NULL);
 ELSE
  -- selection_create body is canonicalized by the browser service, then checked
  -- again here because the restricted DB capability is its own trust boundary.
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>11 OR NOT payload ?& ARRAY['id','key','collection_id','base_document_id','expected_version','all_matching','retain_existing','filters','include_ids','exclude_ids','csrf_hash']
   OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string'
   OR jsonb_typeof(payload->'all_matching') IS DISTINCT FROM 'boolean' OR jsonb_typeof(payload->'retain_existing') IS DISTINCT FROM 'boolean' OR jsonb_typeof(payload->'filters') IS DISTINCT FROM 'object'
   OR (SELECT count(*) FROM jsonb_object_keys(payload->'filters'))<>4 OR NOT (payload->'filters') ?& ARRAY['query','department','job_title','group']
   OR jsonb_typeof(payload->'include_ids') IS DISTINCT FROM 'array' OR jsonb_typeof(payload->'exclude_ids') IS DISTINCT FROM 'array'
   OR jsonb_array_length(payload->'include_ids')>1000 OR jsonb_array_length(payload->'exclude_ids')>1000 THEN RETURN jsonb_build_object('status','invalid'); END IF;
  FOREACH field_name IN ARRAY ARRAY['query','department','job_title','group'] LOOP
   IF payload->'filters'->field_name<>'null'::jsonb AND (jsonb_typeof(payload->'filters'->field_name) IS DISTINCT FROM 'string'
    OR NOT {schema}._organization_label(payload->'filters'->>field_name,CASE WHEN field_name='query' THEN 200 ELSE 100 END,CASE WHEN field_name='query' THEN 800 ELSE 400 END)) THEN RETURN jsonb_build_object('status','invalid'); END IF;
  END LOOP;
  IF (payload->'collection_id'<>'null'::jsonb AND jsonb_typeof(payload->'collection_id') IS DISTINCT FROM 'string')
   OR (payload->'base_document_id'<>'null'::jsonb AND jsonb_typeof(payload->'base_document_id') IS DISTINCT FROM 'string')
   OR (payload->'base_document_id'='null'::jsonb AND payload->'expected_version'<>'null'::jsonb)
   OR (payload->'base_document_id'<>'null'::jsonb AND (jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$')) THEN RETURN jsonb_build_object('status','invalid'); END IF;
  collection:=(payload->>'collection_id')::uuid;base_id:=(payload->>'base_document_id')::uuid;expected:=(payload->>'expected_version')::bigint;
  PERFORM pg_advisory_xact_lock(hashtextextended('eai:knowledge-selection-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
  IF base_id IS NOT NULL THEN
   SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=base_id FOR SHARE;
   IF NOT FOUND OR d.withdrawn_at IS NOT NULL OR d.collection_id IS DISTINCT FROM collection THEN RETURN jsonb_build_object('status','missing'); END IF;
  END IF;
  IF collection IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=collection FOR SHARE; END IF;
  IF NOT {schema}._files_allowed(t,p,'knowledge',collection) THEN RETURN jsonb_build_object('status','missing'); END IF;
  request_hash:=encode(sha256(convert_to((payload-'id'-'csrf_hash')::text,'UTF8')),'hex');
  SELECT * INTO prior FROM {schema}.knowledge_selection_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
  IF prior.key IS NOT NULL THEN
   IF prior.request_hash<>request_hash OR prior.expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('status','conflict'); END IF;
   result:=prior.response;
  ELSE
   IF base_id IS NOT NULL AND d.version<>expected THEN RETURN jsonb_build_object('status','conflict'); END IF;
   IF EXISTS(SELECT 1 FROM jsonb_array_elements(payload->'include_ids') item WHERE jsonb_typeof(item)<>'string')
    OR EXISTS(SELECT 1 FROM jsonb_array_elements(payload->'exclude_ids') item WHERE jsonb_typeof(item)<>'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
   SELECT coalesce(array_agg(value::uuid),ARRAY[]::uuid[]) INTO include_ids FROM jsonb_array_elements_text(payload->'include_ids');
   SELECT coalesce(array_agg(value::uuid),ARRAY[]::uuid[]) INTO exclude_ids FROM jsonb_array_elements_text(payload->'exclude_ids');
   IF EXISTS(SELECT 1 FROM unnest(include_ids) pid WHERE NOT EXISTS(SELECT 1 FROM {schema}.private_organization_members m WHERE m.tenant_id=t AND m.principal_id=pid AND m.membership_active AND m.principal_active)) THEN RETURN jsonb_build_object('status','invalid'); END IF;
   WITH candidates AS (
    SELECT principal_id id FROM {schema}.document_acl WHERE tenant_id=t AND document_id=base_id AND (payload->>'retain_existing')::boolean
    UNION SELECT principal_id FROM {schema}._organization_filtered(t,true,payload->'filters') WHERE (payload->>'all_matching')::boolean
    UNION SELECT unnest(include_ids)
   ), final_ids AS (SELECT id FROM candidates WHERE NOT id=ANY(exclude_ids) UNION SELECT p WHERE base_id IS NULL)
   SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::uuid[]) INTO selected_ids FROM (SELECT id FROM final_ids ORDER BY id LIMIT 1001) q;
   IF cardinality(selected_ids)>1000 THEN RETURN jsonb_build_object('status','capacity'); END IF;
   SELECT coalesce(jsonb_agg(jsonb_build_object('principal_id',person.id,'membership_id',m.membership_id,
    'display_name',coalesce(m.display_name,CASE WHEN {schema}._organization_label(btrim(person.display_name),200,800) THEN btrim(person.display_name) END),
    'membership_active',coalesce(m.membership_active,false),'principal_active',person.active) ORDER BY person.id),'[]'::jsonb) INTO items
    FROM unnest(selected_ids) pid JOIN {schema}.principals person ON person.id=pid
    LEFT JOIN {schema}.private_organization_members m ON m.tenant_id=t AND m.principal_id=pid;
   IF jsonb_array_length(items)<>cardinality(selected_ids) OR octet_length(items::text)>2097152 THEN RETURN jsonb_build_object('status','capacity'); END IF;
   -- Serialize quota admission globally. Cleanup never waits on a live apply;
   -- all document/reader work precedes cleanup to avoid inverse lock waits.
   PERFORM person.id FROM {schema}.principals person WHERE person.id=ANY(selected_ids) ORDER BY person.id FOR SHARE NOWAIT;
   PERFORM m.id FROM {schema}.memberships m WHERE m.tenant_id=t AND m.principal_id=ANY(selected_ids) ORDER BY m.id FOR SHARE NOWAIT;
   PERFORM pg_advisory_xact_lock(hashtextextended('eai:knowledge-selection-quota',0));
   DELETE FROM {schema}.knowledge_selections WHERE (tenant_id,id) IN (
    SELECT tenant_id,id FROM {schema}.knowledge_selections WHERE expires_at<=clock_timestamp() ORDER BY expires_at LIMIT 128 FOR UPDATE SKIP LOCKED);
   IF (SELECT count(*) FROM {schema}.knowledge_selections WHERE tenant_id=t AND actor_id=p)>=8
    OR (SELECT count(*) FROM {schema}.knowledge_selections WHERE tenant_id=t)>=32 OR (SELECT count(*) FROM {schema}.knowledge_selections)>=128 THEN RETURN jsonb_build_object('status','capacity'); END IF;
   moment:=clock_timestamp();
   INSERT INTO {schema}.knowledge_selections(tenant_id,id,actor_id,key,collection_id,base_document_id,expected_document_version,snapshot_digest,items,creator_grant_included,retain_existing,created_at,expires_at)
    VALUES(t,(payload->>'id')::uuid,p,(payload->>'key')::uuid,collection,base_id,expected,encode(sha256(convert_to(items::text,'UTF8')),'hex'),items,base_id IS NULL,(payload->>'retain_existing')::boolean,moment,moment+interval '900 seconds') RETURNING * INTO s;
   -- Verify captured activity again after nonblocking locks; never re-evaluate
   -- organization filters into a different list while the user is reviewing.
   checked:={schema}._knowledge_selection_live(t,p,s.id,collection,base_id,expected);
   IF checked->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Selection changed.' USING ERRCODE='P0002'; END IF;
   result:={schema}._knowledge_selection_receipt(s);
   INSERT INTO {schema}.knowledge_selection_requests(tenant_id,actor_id,key,request_hash,response,expires_at) VALUES(t,p,(payload->>'key')::uuid,request_hash,result,s.expires_at);
  END IF;
 END IF;
 IF operation='audience_replace' AND checked IS NOT NULL AND extract(epoch FROM clock_timestamp())>=(checked->>'expires_at')::numeric THEN
  RAISE EXCEPTION 'Selection expired.' USING ERRCODE='P0002';
 END IF;
 context:={schema}.authorization_call(token,'finish',CASE WHEN operation IN ('audience_replace','selection_create') THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Knowledge authority expired.' USING ERRCODE='40001'; END IF;
 RETURN result||jsonb_build_object('status','ok','authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN no_data_found OR lock_not_available THEN RETURN jsonb_build_object('status','conflict');
 WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation
 OR foreign_key_violation OR invalid_parameter_value THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}._knowledge_access(text,text,jsonb) FROM PUBLIC,{access_role};

ALTER FUNCTION {schema}.knowledge_call(text,text,jsonb) RENAME TO _knowledge_v18_call;
REVOKE ALL ON FUNCTION {schema}._knowledge_v18_call(text,text,jsonb) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}.knowledge_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;prior {schema}.knowledge_requests%ROWTYPE;e {schema}.knowledge_events%ROWTYPE;
 d {schema}.documents%ROWTYPE;result jsonb;checked jsonb;material jsonb;mode text;selection uuid;actual_collection uuid;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Knowledge capability rejected.'; END IF;
 IF operation IN ('audience','audience_replace','selection_create','selection') THEN RETURN {schema}._knowledge_access(token,operation,payload); END IF;
 IF operation IS NULL OR operation NOT IN ('create','acl') THEN RETURN {schema}._knowledge_v18_call(token,operation,payload); END IF;
 IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>16600 THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 PERFORM pg_advisory_xact_lock(hashtextextended('eai:knowledge-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
 IF operation='acl' THEN
  SELECT * INTO e FROM {schema}.knowledge_events WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
  result:={schema}._knowledge_v18_call(token,operation,payload);
  IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
  -- A new legacy full replacement is restricted. Replaying an old ACL receipt
  -- must not undo a later switch to all-employees.
  IF e.key IS NULL THEN UPDATE {schema}.documents SET audience_mode='specific_people' WHERE tenant_id=t AND id=(result->>'document_id')::uuid; END IF;
 ELSE
  IF (payload ? 'audience_mode' AND payload->'audience_mode'<>'null'::jsonb AND (jsonb_typeof(payload->'audience_mode') IS DISTINCT FROM 'string' OR payload->>'audience_mode' NOT IN ('specific_people','all_current_employees')))
   OR (payload ? 'selection_id' AND payload->'selection_id'<>'null'::jsonb AND jsonb_typeof(payload->'selection_id') IS DISTINCT FROM 'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
  mode:=coalesce(payload->>'audience_mode','specific_people');selection:=(payload->>'selection_id')::uuid;
  IF selection IS NOT NULL AND payload->>'audience_mode' IS DISTINCT FROM 'specific_people' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF (mode='all_current_employees' OR selection IS NOT NULL) AND payload->'readers' IS DISTINCT FROM '[]'::jsonb THEN RETURN jsonb_build_object('status','invalid'); END IF;
  material:=CASE WHEN mode='specific_people' AND selection IS NULL THEN '{{}}'::jsonb ELSE jsonb_strip_nulls(jsonb_build_object('audience_mode',mode,'selection_id',selection)) END;
  SELECT * INTO prior FROM {schema}.knowledge_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
  IF prior.key IS NOT NULL THEN
   -- Base admission reauthorizes ordinary file/document material and period.
   -- Committed receipt replay deliberately does not require a live snapshot.
   result:={schema}._knowledge_v18_call(token,operation,payload-'audience_mode'-'selection_id');
   IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
   IF prior.audience_material IS DISTINCT FROM material THEN RETURN jsonb_build_object('status','conflict'); END IF;
   IF prior.audience_receipt IS NOT NULL THEN result:=prior.audience_receipt; END IF;
  ELSE
   IF selection IS NOT NULL THEN
    -- Determine the actual file collection, never a free tenant/collection claim.
    SELECT f.collection_id INTO actual_collection FROM {schema}.files f WHERE f.tenant_id=t AND f.id=(payload->>'file_id')::uuid AND f.owner_principal_id=p FOR SHARE;
    IF NOT FOUND OR NOT {schema}._files_allowed(t,p,'knowledge',actual_collection) THEN RETURN jsonb_build_object('status','missing'); END IF;
    IF actual_collection IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=actual_collection FOR SHARE; END IF;
    checked:={schema}._knowledge_selection_live(t,p,selection,actual_collection,NULL,NULL);
    IF checked->>'status' IS DISTINCT FROM 'ok' THEN RETURN checked; END IF;
   END IF;
   result:={schema}._knowledge_v18_call(token,operation,payload-'audience_mode'-'selection_id');
   IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result; END IF;
   IF material<>'{{}}'::jsonb THEN
    SELECT * INTO d FROM {schema}.documents WHERE tenant_id=t AND id=(result->>'document_id')::uuid FOR UPDATE;
    DELETE FROM {schema}.document_acl WHERE tenant_id=t AND document_id=d.id;
    IF selection IS NOT NULL THEN
     INSERT INTO {schema}.document_acl(tenant_id,document_id,principal_id) SELECT t,d.id,(value->>'principal_id')::uuid FROM jsonb_array_elements(checked->'items');
     IF extract(epoch FROM clock_timestamp())>=(checked->>'expires_at')::numeric THEN RAISE EXCEPTION 'Selection expired.' USING ERRCODE='P0002'; END IF;
     UPDATE {schema}.knowledge_selections SET consumed_document_id=d.id,consumed_key=(payload->>'key')::uuid WHERE tenant_id=t AND id=selection;
    END IF;
    UPDATE {schema}.documents SET audience_mode=mode WHERE tenant_id=t AND id=d.id;
    result:=result-'authority_expires_at'||jsonb_build_object('audience_mode',mode,'explicit_readers_count',(SELECT count(*) FROM {schema}.document_acl WHERE tenant_id=t AND document_id=d.id));
    UPDATE {schema}.knowledge_requests SET audience_material=material,audience_receipt=result WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
   END IF;
  END IF;
 END IF;
 IF operation='create' AND checked IS NOT NULL AND extract(epoch FROM clock_timestamp())>=(checked->>'expires_at')::numeric THEN
  RAISE EXCEPTION 'Selection expired.' USING ERRCODE='P0002';
 END IF;
 context:={schema}.authorization_call(token,'finish',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Knowledge authority expired.' USING ERRCODE='40001'; END IF;
 RETURN result||jsonb_build_object('authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN no_data_found OR lock_not_available THEN RETURN jsonb_build_object('status','conflict');
 WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation
 OR foreign_key_violation OR invalid_parameter_value THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.knowledge_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.knowledge_call(text,text,jsonb) TO {access_role};

-- New private snapshot/creation-key tables; no additional public capability.
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='browser_context_guard' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, expected text')
                        AND NOT (f.proname='organization_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
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



