-- FLOW-02: private assisted requests; never a model business capability.
CREATE TABLE {schema}.assisted_requests (
 tenant_id uuid NOT NULL,run_id uuid NOT NULL,conversation_id uuid NOT NULL,actor_id uuid NOT NULL,
 membership_id uuid NOT NULL,session_id uuid NOT NULL,access_revision bigint NOT NULL,
 selection jsonb NOT NULL CHECK(jsonb_typeof(selection)='object' AND octet_length(selection::text)<=16384),
 proposal jsonb CHECK(proposal IS NULL OR (jsonb_typeof(proposal)='object' AND octet_length(proposal::text)<=8192)),
 draft_id uuid,PRIMARY KEY(tenant_id,run_id),
 FOREIGN KEY(tenant_id,run_id,conversation_id) REFERENCES {schema}.runs(tenant_id,id,conversation_id),
 FOREIGN KEY(tenant_id,draft_id) REFERENCES {schema}.drafts(tenant_id,id)
);
ALTER TABLE {schema}.assisted_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.assisted_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.assisted_requests TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.assisted_requests FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._assisted_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(NEW)-ARRAY['proposal','draft_id']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['proposal','draft_id'])
  OR (OLD.proposal IS NOT NULL AND NEW.proposal IS DISTINCT FROM OLD.proposal)
  OR (OLD.draft_id IS NOT NULL AND NEW.draft_id IS DISTINCT FROM OLD.draft_id)
 THEN RAISE EXCEPTION 'Assisted identity is immutable' USING ERRCODE='23514';END IF;RETURN NEW;
END $$;
CREATE TRIGGER assisted_immutable BEFORE UPDATE OR DELETE ON {schema}.assisted_requests FOR EACH ROW EXECUTE FUNCTION {schema}._assisted_immutable();
REVOKE ALL ON FUNCTION {schema}._assisted_immutable() FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.assisted_owner_call(token text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE result jsonb;r {schema}.runs%ROWTYPE;q {schema}.assisted_requests%ROWTYPE;selected jsonb:=payload->'selection';
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Assisted capability rejected';END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(payload))=1 AND payload ? 'id' THEN
  result:={schema}.checkpoint_owner_call(token,'read',payload);
  IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result;END IF;
  SELECT * INTO q FROM {schema}.assisted_requests WHERE run_id=(payload->>'id')::uuid AND tenant_id=(SELECT active_tenant_id FROM {schema}.sessions WHERE token_hash=token);
  IF q.run_id IS NULL THEN RETURN jsonb_build_object('status','missing');END IF;
  RETURN jsonb_build_object('status','ok','run_id',q.run_id,'draft_id',q.draft_id,'clarification_reason',CASE WHEN q.proposal->>'name'='clarify_reimbursement' THEN q.proposal->'input'->>'reason' END,'authority_expires_at',result->'authority_expires_at');
 END IF;
 IF jsonb_typeof(selected) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(selected))<>4 OR NOT selected ?& ARRAY['kind','trip_id','claim_id','receipts']
  OR selected->>'kind' IS NULL OR selected->>'kind' NOT IN ('submission','information_update') OR (selected->>'trip_id')::uuid IS NULL
  OR jsonb_typeof(selected->'receipts') IS DISTINCT FROM 'array' OR jsonb_array_length(selected->'receipts')>20
  OR (selected->>'kind'='submission' AND selected->'claim_id' IS DISTINCT FROM 'null'::jsonb)
  OR (selected->>'kind'='information_update' AND (selected->>'claim_id')::uuid IS NULL) THEN RETURN jsonb_build_object('status','invalid');END IF;
 result:={schema}.conversation_call(token,'append',payload-'selection');
 IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result;END IF;
 SELECT * INTO r FROM {schema}.runs WHERE id=(result->>'run_id')::uuid AND conversation_id=(payload->>'id')::uuid FOR UPDATE;
 SELECT * INTO q FROM {schema}.assisted_requests WHERE tenant_id=r.tenant_id AND run_id=r.id;
 IF FOUND THEN
  IF q.selection IS DISTINCT FROM selected THEN RETURN jsonb_build_object('status','conflict');END IF;
 ELSE
  IF r.state<>'queued' OR r.model_ref IS NOT NULL THEN RETURN jsonb_build_object('status','conflict');END IF;
  INSERT INTO {schema}.assisted_requests(tenant_id,run_id,conversation_id,actor_id,membership_id,session_id,access_revision,selection)
   VALUES(r.tenant_id,r.id,r.conversation_id,r.actor_id,r.membership_id,r.session_id,r.access_revision,selected);
 END IF;
 RETURN result;
EXCEPTION WHEN data_exception OR check_violation THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.assisted_owner_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.assisted_owner_call(text,jsonb) TO {access_role};

CREATE FUNCTION {schema}.assisted_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE bound jsonb;context jsonb;result jsonb;args jsonb;h jsonb;act jsonb;
 r {schema}.runs%ROWTYPE;q {schema}.assisted_requests%ROWTYPE;s {schema}.sessions%ROWTYPE;d {schema}.drafts%ROWTYPE;a {schema}.actions%ROWTYPE;
 deadline numeric;key uuid;candidate uuid;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Assisted capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('input','proposal','create','replace','preview','status') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>131072
  OR NOT payload ?& ARRAY['credential_hash','tenant_id','run_id','job_id','generation','epoch'] THEN RETURN jsonb_build_object('status','invalid');END IF;
 bound:={schema}.checkpoint_call('stage',jsonb_build_object('credential_hash',payload->'credential_hash','tenant_id',payload->'tenant_id','run_id',payload->'run_id','job_id',payload->'job_id','generation',payload->'generation','epoch',payload->'epoch','sources','[]'::jsonb,'namespace','','checkpoint_id',NULL));
 IF bound->>'status' IS DISTINCT FROM 'ok' THEN RETURN bound;END IF;
 SELECT * INTO r FROM {schema}.runs WHERE tenant_id=(payload->>'tenant_id')::uuid AND id=(payload->>'run_id')::uuid FOR UPDATE;
 SELECT * INTO q FROM {schema}.assisted_requests WHERE tenant_id=r.tenant_id AND run_id=r.id FOR UPDATE;
 IF q.run_id IS NULL OR r.state<>'running' OR r.writer_generation IS DISTINCT FROM (payload->>'epoch')::bigint
  OR ROW(r.actor_id,r.membership_id,r.session_id,r.access_revision) IS DISTINCT FROM ROW(q.actor_id,q.membership_id,q.session_id,q.access_revision) THEN RETURN jsonb_build_object('status','stale');END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE id=q.session_id FOR SHARE;
 deadline:=(bound->>'authority_expires_at')::numeric;
 context:=jsonb_build_object('tenant_id',r.tenant_id,'actor_id',r.actor_id,'membership_id',r.membership_id,'session_id',r.session_id,'access_revision',r.access_revision,'authority_expires_at',deadline);
 IF operation='input' THEN
  result:=jsonb_build_object('context',context,'selection',q.selection,'proposal',q.proposal,'question',(SELECT body FROM {schema}.messages WHERE tenant_id=r.tenant_id AND id=r.input_message_id),'conversation_id',r.conversation_id);
 ELSIF operation='proposal' THEN
  IF q.proposal IS NOT NULL OR jsonb_typeof(payload->'proposal') IS DISTINCT FROM 'object' OR octet_length((payload->'proposal')::text)>8192
   OR payload->'proposal'->>'name' IS NULL OR payload->'proposal'->>'name' NOT IN ('prepare_reimbursement','clarify_reimbursement')
   OR NOT EXISTS(SELECT 1 FROM {schema}.rag_attempts WHERE tenant_id=r.tenant_id AND run_id=r.id AND kind='chat' AND state='known' AND input_tokens<=reserved_input_tokens AND output_tokens<=reserved_output_tokens) THEN RETURN jsonb_build_object('status','conflict');END IF;
  UPDATE {schema}.assisted_requests SET proposal=payload->'proposal' WHERE tenant_id=r.tenant_id AND run_id=r.id;
  result:='{{}}'::jsonb;
 ELSIF operation='status' THEN
  SELECT * INTO d FROM {schema}.drafts WHERE tenant_id=q.tenant_id AND id=q.draft_id AND owner_principal_id=q.actor_id AND conversation_id=q.conversation_id FOR SHARE;
  SELECT aa.* INTO a FROM {schema}.actions aa JOIN {schema}.consents c ON c.tenant_id=aa.tenant_id AND c.operation_id=aa.id
   JOIN {schema}.human_action_receipts hr ON hr.tenant_id=aa.tenant_id AND hr.id=aa.human_receipt_id
   JOIN {schema}.draft_previews pp ON pp.tenant_id=c.tenant_id AND pp.draft_id=c.draft_id AND pp.version=c.draft_version AND pp.material_hash=c.material_hash
   WHERE aa.tenant_id=q.tenant_id AND aa.draft_id=d.id AND aa.draft_version=d.current_version
    AND aa.actor_id=q.actor_id AND aa.membership_id=q.membership_id AND aa.session_id=q.session_id AND aa.access_revision=q.access_revision
    AND aa.kind=CASE q.selection->>'kind' WHEN 'submission' THEN 'claim.submit' ELSE 'claim.information' END
    AND c.actor_id=aa.actor_id AND c.session_id=aa.session_id AND c.draft_id=aa.draft_id AND c.draft_version=aa.draft_version AND c.material_hash=aa.material_hash AND c.human_receipt_id=aa.human_receipt_id
    AND hr.actor_id=aa.actor_id AND hr.session_id=aa.session_id AND hr.operation_id=aa.id AND hr.action=aa.kind AND NOT hr.revoked AND hr.request_hash=aa.canonical_hash FOR SHARE OF aa,c,hr,pp;
  -- A confirmed human edit may supersede the agent proposal. ACT independently
  -- binds that exact current version/hash; the model never supplies an action ID.
  result:=jsonb_build_object('action',CASE WHEN a.id IS NOT NULL THEN jsonb_build_object('operation_id',a.id,'state',a.state,'draft_version',a.draft_version,'material_hash',a.material_hash) END);
 ELSE
  IF q.proposal->>'name' IS DISTINCT FROM 'prepare_reimbursement' THEN RETURN jsonb_build_object('status','forbidden');END IF;
  candidate:=(md5('assisted:draft:'||r.id::text))::uuid;
  IF operation='create' THEN
   key:=(md5('assisted:create:'||r.id::text))::uuid;
   args:=jsonb_build_object('kind',q.selection->'kind','trip_id',q.selection->'trip_id','claim_id',q.selection->'claim_id','conversation_id',r.conversation_id,'key',key,'csrf_hash',s.csrf_hash,'source',payload->'source','id',candidate);
   result:={schema}.draft_call(s.token_hash,'create',args);
   IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result;END IF;
   IF result->'draft'<>'null'::jsonb THEN UPDATE {schema}.assisted_requests SET draft_id=candidate WHERE tenant_id=r.tenant_id AND run_id=r.id;END IF;
  ELSE
   SELECT * INTO d FROM {schema}.drafts WHERE tenant_id=q.tenant_id AND id=q.draft_id AND id=candidate AND owner_principal_id=q.actor_id AND conversation_id=q.conversation_id FOR SHARE;
   IF d.id IS NULL THEN RETURN jsonb_build_object('status','missing');END IF;
   IF operation='replace' THEN
    key:=(md5('assisted:replace:'||r.id::text))::uuid;
    args:=jsonb_build_object('id',d.id,'expected_version',1,'key',key,'content',payload->'content','csrf_hash',s.csrf_hash);
   ELSE
    args:=jsonb_build_object('id',d.id,'expected_version',d.current_version,'source',payload->'source','csrf_hash',s.csrf_hash);
   END IF;
   result:={schema}.draft_call(s.token_hash,operation,args);
   IF result->>'status' IS DISTINCT FROM 'ok' THEN RETURN result;END IF;
  END IF;
 END IF;
 IF extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Assisted authority expired' USING ERRCODE='40001';END IF;
 RETURN coalesce(result,'{{}}'::jsonb)||jsonb_build_object('status','ok','authority_expires_at',deadline);
EXCEPTION WHEN data_exception OR check_violation THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.assisted_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.assisted_call(text,jsonb) TO {access_role};

DO $$ DECLARE name text;BEGIN
 SELECT c.conname INTO STRICT name FROM pg_constraint c WHERE c.conrelid='{schema}.source_dependencies'::regclass AND c.contype='c' AND pg_get_constraintdef(c.oid) LIKE '%platform_document_version_id%' AND pg_get_constraintdef(c.oid) LIKE '%source_system%';
 EXECUTE format('ALTER TABLE {schema}.source_dependencies DROP CONSTRAINT %I',name);
END $$;
ALTER TABLE {schema}.source_dependencies ADD CHECK ((source_system='platform' AND resource_type='document_version' AND required_relation='read' AND platform_document_version_id IS NOT NULL AND platform_document_version_id=resource_id AND platform_file_id IS NULL) OR
       (source_system='platform' AND resource_type='file' AND required_relation='read' AND platform_file_id IS NOT NULL AND platform_file_id=resource_id AND platform_document_version_id IS NULL) OR
       (source_system='service' AND platform_document_version_id IS NULL AND platform_file_id IS NULL AND
          ((resource_type='trip' AND required_relation='owner') OR (resource_type IN ('claim','receipt') AND required_relation IN ('owner','active_reviewer')))) OR (source_system='platform' AND resource_type='assisted_request' AND required_relation='owner' AND resource_version='1' AND observed_revision='1' AND platform_document_version_id IS NULL AND platform_file_id IS NULL AND page IS NULL AND chunk IS NULL));

CREATE OR REPLACE FUNCTION {schema}.document_sources_call(auth jsonb,refs jsonb,history boolean) RETURNS jsonb
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
  IF item->>'system'='platform' AND item->>'kind'='assisted_request' AND item->>'relation'='owner' AND item->>'version'='1' AND item->>'observed_revision'='1' AND item->>'page' IS NULL AND item->>'chunk' IS NULL THEN
   PERFORM q.run_id FROM {schema}.assisted_requests q JOIN {schema}.conversations cc ON cc.tenant_id=q.tenant_id AND cc.id=q.conversation_id
    WHERE q.tenant_id=t AND q.actor_id=p AND q.run_id=(item->>'resource_id')::uuid AND cc.owner_principal_id=p FOR SHARE OF q,cc;
   allowed:=FOUND;
  END IF;
  decisions:=decisions||jsonb_build_array(coalesce(allowed,false));
 END LOOP;
 IF extract(epoch FROM clock_timestamp())>=deadline THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
 RETURN jsonb_build_object('status','ok','tenant_id',t,'principal_id',p,'decisions',decisions,'authority_expires_at',deadline);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN jsonb_build_object('status','invalid');
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='trip_catalog_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text')
                        AND NOT (f.proname='policy_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='policy_eligibility_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='auth jsonb, request jsonb')
                        AND NOT (f.proname='receipt_pin_prepare' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='auth jsonb, request jsonb')
                        AND NOT (f.proname='receipt_file_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='flow_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='flow_locked_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='actor jsonb, payload jsonb, policy jsonb, trip_date date, claim_id uuid')
                        AND NOT (f.proname='assisted_owner_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, payload jsonb')
                        AND NOT (f.proname='assisted_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='operation text, payload jsonb')
                        AND NOT (f.proname='action_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='human_receipt_get' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='receipt uuid')
                        AND NOT (f.proname='draft_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='receipt_pin_lookup' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='credential text, tenant uuid, operation uuid')
                        AND NOT (f.proname='receipt_pin_finalise' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='credential text, settlement jsonb')
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



