-- BD-FE-01: tenant-local descriptive attributes, never identity/role provisioning.
CREATE FUNCTION {schema}._organization_label(value text,max_chars integer,max_bytes integer) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT value IS NULL OR (length(value) BETWEEN 1 AND max_chars AND octet_length(value)<=max_bytes
  AND value=btrim(value) AND value !~ '[[:cntrl:]]');
$$;
REVOKE ALL ON FUNCTION {schema}._organization_label(text,integer,integer) FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._organization_groups(value text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT cardinality(value)<=32 AND NOT EXISTS(SELECT 1 FROM unnest(value) item WHERE item IS NULL OR NOT {schema}._organization_label(item,100,400))
  AND cardinality(value)=(SELECT count(DISTINCT item) FROM unnest(value) item);
$$;
REVOKE ALL ON FUNCTION {schema}._organization_groups(text[]) FROM PUBLIC,{access_role};
CREATE TABLE {schema}.organization_profiles (
 tenant_id uuid NOT NULL,membership_id uuid NOT NULL,display_name text,department text,job_title text,groups text[] NOT NULL DEFAULT ARRAY[]::text[],
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,membership_id),FOREIGN KEY(tenant_id,membership_id) REFERENCES {schema}.memberships(tenant_id,id),
 CHECK({schema}._organization_label(display_name,200,800)),CHECK({schema}._organization_label(department,100,400)),
 CHECK({schema}._organization_label(job_title,100,400)),CHECK({schema}._organization_groups(groups))
);
CREATE INDEX organization_department ON {schema}.organization_profiles(tenant_id,department,membership_id);
CREATE INDEX organization_title ON {schema}.organization_profiles(tenant_id,job_title,membership_id);
CREATE INDEX organization_groups ON {schema}.organization_profiles USING gin(groups);
CREATE TABLE {schema}.organization_requests (
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,membership_id uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),response jsonb NOT NULL CHECK(octet_length(response::text)<=16384),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,actor_id,key),
 FOREIGN KEY(tenant_id,actor_id) REFERENCES {schema}.memberships(tenant_id,principal_id),
 FOREIGN KEY(tenant_id,membership_id) REFERENCES {schema}.memberships(tenant_id,id)
);
ALTER TABLE {schema}.organization_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.organization_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.organization_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.organization_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.organization_profiles TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.organization_requests TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.organization_profiles,{schema}.organization_requests FROM PUBLIC,{access_role};
CREATE VIEW {schema}.private_organization_members AS
 SELECT m.tenant_id,m.id membership_id,m.principal_id,coalesce(o.display_name,
  CASE WHEN {schema}._organization_label(btrim(p.display_name),200,800) THEN btrim(p.display_name) END) display_name,
  o.display_name local_display_name,o.department,o.job_title,coalesce(o.groups,ARRAY[]::text[]) groups,
  coalesce(o.version,1) organization_version,m.active membership_active,p.active principal_active
 FROM {schema}.memberships m JOIN {schema}.principals p ON p.id=m.principal_id
 LEFT JOIN {schema}.organization_profiles o ON o.tenant_id=m.tenant_id AND o.membership_id=m.id;
REVOKE ALL ON {schema}.private_organization_members FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._organization_filtered(t uuid,active_only boolean,payload jsonb)
RETURNS SETOF {schema}.private_organization_members LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT m.* FROM {schema}.private_organization_members m WHERE m.tenant_id=t
  AND (NOT active_only OR m.membership_active AND m.principal_active)
  AND (payload->>'query' IS NULL OR strpos(lower(coalesce(m.display_name,'')),lower(payload->>'query'))>0 OR strpos(m.principal_id::text,lower(payload->>'query'))>0)
  AND (payload->>'department' IS NULL OR m.department=payload->>'department')
  AND (payload->>'job_title' IS NULL OR m.job_title=payload->>'job_title')
  AND (payload->>'group' IS NULL OR m.groups @> ARRAY[payload->>'group']);
$$;
REVOKE ALL ON FUNCTION {schema}._organization_filtered(uuid,boolean,jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.organization_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE context jsonb;identity jsonb;t uuid;p uuid;admin_allowed boolean;picker boolean;collection uuid;target uuid;
 person {schema}.private_organization_members%ROWTYPE;profile {schema}.organization_profiles%ROWTYPE;
 prior {schema}.organization_requests%ROWTYPE;result jsonb;items jsonb;request_hash text;limit_rows integer;cursor_value text;
 group_values text[];field_name text;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Organization capability rejected.'; END IF;
 IF operation IS NULL OR operation NOT IN ('directory','member','replace','facets','collections') OR payload IS NULL
  OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>(CASE WHEN operation='replace' THEN 16384 ELSE 4096 END) THEN RETURN jsonb_build_object('status','invalid'); END IF;
 context:={schema}.authorization_call(token,'context',CASE WHEN operation='replace' THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RETURN context; END IF;
 identity:=context->'identity';t:=(identity->>'active_tenant_id')::uuid;p:=(identity->>'principal_id')::uuid;
 admin_allowed:=EXISTS(SELECT 1 FROM jsonb_array_elements(context->'grants') g WHERE g->>'role'='tenant_admin' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text);
 IF operation IN ('member','replace') THEN
  IF NOT admin_allowed THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF NOT payload ? 'id' OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid'); END IF;
  target:=(payload->>'id')::uuid;
  -- Descriptive attributes do not update the membership. SHARE avoids reciprocal
  -- admin-edit deadlocks against membership SHARE locks held by authorization.
  PERFORM id FROM {schema}.memberships WHERE tenant_id=t AND id=target FOR SHARE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
  IF operation='member' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid'); END IF;
  ELSE
   IF (SELECT count(*) FROM jsonb_object_keys(payload))<>8 OR NOT payload ?& ARRAY['id','key','expected_version','display_name','department','job_title','groups','csrf_hash']
    OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'expected_version') IS DISTINCT FROM 'number'
    OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$' OR jsonb_typeof(payload->'groups') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'groups')>32
    THEN RETURN jsonb_build_object('status','invalid'); END IF;
   FOREACH field_name IN ARRAY ARRAY['display_name','department','job_title'] LOOP
    IF payload->field_name<>'null'::jsonb AND (jsonb_typeof(payload->field_name) IS DISTINCT FROM 'string'
     OR NOT {schema}._organization_label(payload->>field_name,CASE WHEN field_name='display_name' THEN 200 ELSE 100 END,CASE WHEN field_name='display_name' THEN 800 ELSE 400 END)) THEN RETURN jsonb_build_object('status','invalid'); END IF;
   END LOOP;
   IF EXISTS(SELECT 1 FROM jsonb_array_elements(payload->'groups') item WHERE jsonb_typeof(item)<>'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
   SELECT coalesce(array_agg(value ORDER BY value COLLATE "C"),ARRAY[]::text[]) INTO group_values FROM jsonb_array_elements_text(payload->'groups');
   IF NOT {schema}._organization_groups(group_values) THEN RETURN jsonb_build_object('status','invalid'); END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended('eai:organization-key:'||t::text||':'||p::text||':'||((payload->>'key')::uuid)::text,0));
   PERFORM pg_advisory_xact_lock(hashtextextended('eai:organization-profile:'||t::text||':'||target::text,0));
   request_hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
   SELECT * INTO prior FROM {schema}.organization_requests WHERE tenant_id=t AND actor_id=p AND key=(payload->>'key')::uuid;
   IF prior.key IS NOT NULL THEN
    IF prior.request_hash<>request_hash THEN RETURN jsonb_build_object('status','conflict'); END IF;
    result:=prior.response;
   ELSE
    SELECT * INTO profile FROM {schema}.organization_profiles WHERE tenant_id=t AND membership_id=target FOR UPDATE;
    IF coalesce(profile.version,1)<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
    INSERT INTO {schema}.organization_profiles(tenant_id,membership_id,display_name,department,job_title,groups,version)
     VALUES(t,target,payload->>'display_name',payload->>'department',payload->>'job_title',group_values,coalesce(profile.version,1)+1)
     ON CONFLICT(tenant_id,membership_id) DO UPDATE SET display_name=excluded.display_name,department=excluded.department,job_title=excluded.job_title,groups=excluded.groups,version=excluded.version,updated_at=clock_timestamp();
    SELECT * INTO person FROM {schema}.private_organization_members WHERE tenant_id=t AND membership_id=target;
    result:=to_jsonb(person)-'tenant_id';
    INSERT INTO {schema}.organization_requests(tenant_id,actor_id,key,membership_id,request_hash,response) VALUES(t,p,(payload->>'key')::uuid,target,request_hash,result);
   END IF;
  END IF;
  IF operation='member' THEN SELECT * INTO person FROM {schema}.private_organization_members WHERE tenant_id=t AND membership_id=target;result:=to_jsonb(person)-'tenant_id'; END IF;
 ELSIF operation='collections' THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>2 OR NOT payload ?& ARRAY['after','limit']
   OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^([1-9]|[1-4][0-9]|50)$'
   OR (payload->'after'<>'null'::jsonb AND jsonb_typeof(payload->'after') IS DISTINCT FROM 'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
  IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(context->'grants') g WHERE g->>'role'='knowledge_manager') THEN RETURN jsonb_build_object('status','forbidden'); END IF;
  limit_rows:=(payload->>'limit')::integer;
  IF payload->>'after' IS NOT NULL THEN
   PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=(payload->>'after')::uuid AND active AND {schema}._files_allowed(t,p,'knowledge',scope_ref) FOR SHARE;
   IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
  END IF;
  SELECT coalesce(jsonb_agg(value ORDER BY scope_ref),'[]'::jsonb) INTO items FROM (
   SELECT jsonb_build_object('collection_id',s.scope_ref,'display_name',NULL) value,s.scope_ref FROM {schema}.authorization_scopes s WHERE s.tenant_id=t AND s.active
    AND {schema}._files_allowed(t,p,'knowledge',s.scope_ref) AND (payload->>'after' IS NULL OR s.scope_ref>(payload->>'after')::uuid)
    ORDER BY s.scope_ref LIMIT limit_rows+1 FOR SHARE OF s) q;
  IF jsonb_array_length(items)>limit_rows THEN items:=items-limit_rows;cursor_value:=items->(limit_rows-1)->>'collection_id'; END IF;
  result:=jsonb_build_object('tenant_level_allowed',{schema}._files_allowed(t,p,'knowledge',NULL),'items',items,'next_after',cursor_value);
 ELSE
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>(CASE WHEN operation='facets' THEN 9 ELSE 8 END)
   OR NOT payload ?& ARRAY['mode','collection_id','query','department','job_title','group','after','limit']
   OR jsonb_typeof(payload->'mode') IS DISTINCT FROM 'string' OR payload->>'mode' NOT IN ('admin','picker')
   OR (payload->'collection_id'<>'null'::jsonb AND jsonb_typeof(payload->'collection_id') IS DISTINCT FROM 'string')
   OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^([1-9]|[1-4][0-9]|50)$'
   OR (payload->'after'<>'null'::jsonb AND jsonb_typeof(payload->'after') IS DISTINCT FROM 'string') THEN RETURN jsonb_build_object('status','invalid'); END IF;
  picker:=payload->>'mode'='picker';limit_rows:=(payload->>'limit')::integer;collection:=(payload->>'collection_id')::uuid;
  IF picker THEN
   IF collection IS NOT NULL THEN PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=t AND scope_ref=collection FOR SHARE; END IF;
   IF NOT {schema}._files_allowed(t,p,'knowledge',collection) THEN RETURN jsonb_build_object('status',CASE WHEN collection IS NULL THEN 'forbidden' ELSE 'missing' END); END IF;
  ELSIF NOT admin_allowed THEN RETURN jsonb_build_object('status','forbidden'); END IF;
  IF NOT picker AND payload->>'collection_id' IS NOT NULL THEN RETURN jsonb_build_object('status','invalid'); END IF;
  FOREACH field_name IN ARRAY ARRAY['query','department','job_title','group'] LOOP
   IF payload->field_name<>'null'::jsonb AND (jsonb_typeof(payload->field_name) IS DISTINCT FROM 'string'
    OR NOT {schema}._organization_label(payload->>field_name,CASE WHEN field_name='query' THEN 200 ELSE 100 END,CASE WHEN field_name='query' THEN 800 ELSE 400 END)) THEN RETURN jsonb_build_object('status','invalid'); END IF;
  END LOOP;
  IF operation='directory' THEN
   IF payload->>'after' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}._organization_filtered(t,picker,payload) m WHERE m.membership_id=(payload->>'after')::uuid) THEN RETURN jsonb_build_object('status','missing'); END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY membership_id),'[]'::jsonb) INTO items FROM (
    SELECT to_jsonb(m)-'tenant_id'-CASE WHEN picker THEN 'local_display_name' ELSE 'tenant_id' END value,m.membership_id
     FROM {schema}._organization_filtered(t,picker,payload) m WHERE payload->>'after' IS NULL OR m.membership_id>(payload->>'after')::uuid ORDER BY m.membership_id LIMIT limit_rows+1) q;
   IF jsonb_array_length(items)>limit_rows THEN items:=items-limit_rows;cursor_value:=items->(limit_rows-1)->>'membership_id'; END IF;
  ELSE
   IF jsonb_typeof(payload->'field') IS DISTINCT FROM 'string' OR payload->>'field' NOT IN ('department','job_title','group') THEN RETURN jsonb_build_object('status','invalid'); END IF;
   IF payload->>'after' IS NOT NULL AND NOT {schema}._organization_label(payload->>'after',100,400) THEN RETURN jsonb_build_object('status','invalid'); END IF;
   IF payload->>'after' IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}._organization_filtered(t,picker,payload) m,
    LATERAL unnest(CASE payload->>'field' WHEN 'department' THEN ARRAY[m.department] WHEN 'job_title' THEN ARRAY[m.job_title] ELSE m.groups END) value
    WHERE value=payload->>'after') THEN RETURN jsonb_build_object('status','missing'); END IF;
   SELECT coalesce(jsonb_agg(jsonb_build_object('value',value,'count',count_rows) ORDER BY value COLLATE "C"),'[]'::jsonb) INTO items FROM (
    SELECT value,count(*) count_rows FROM {schema}._organization_filtered(t,picker,payload) m,
     LATERAL unnest(CASE payload->>'field' WHEN 'department' THEN ARRAY[m.department] WHEN 'job_title' THEN ARRAY[m.job_title] ELSE m.groups END) value
     WHERE value IS NOT NULL AND (payload->>'after' IS NULL OR value COLLATE "C">(payload->>'after') COLLATE "C")
     GROUP BY value ORDER BY value COLLATE "C" LIMIT limit_rows+1) q;
   IF jsonb_array_length(items)>limit_rows THEN items:=items-limit_rows;cursor_value:=items->(limit_rows-1)->>'value'; END IF;
  END IF;
  result:=jsonb_build_object('items',items,'next_after',cursor_value);
 END IF;
 context:={schema}.authorization_call(token,'finish',CASE WHEN operation='replace' THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF context->>'status' IS DISTINCT FROM 'ok' THEN RAISE EXCEPTION 'Organization authority expired.' USING ERRCODE='40001'; END IF;
 RETURN result||jsonb_build_object('status','ok','authority_expires_at',
  (SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) FROM {schema}.sessions WHERE token_hash=token));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR not_null_violation OR check_violation OR unique_violation
 OR foreign_key_violation OR invalid_parameter_value THEN RETURN jsonb_build_object('status','invalid');
END;
$$;
REVOKE ALL ON FUNCTION {schema}.organization_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.organization_call(text,text,jsonb) TO {access_role};

-- Exact organization capability; private tables/view/helpers stay inaccessible.
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



