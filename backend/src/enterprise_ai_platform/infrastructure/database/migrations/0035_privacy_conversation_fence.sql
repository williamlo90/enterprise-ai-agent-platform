-- DATA01: a durable conversation growth fence, not permission to erase content.
CREATE TABLE {schema}.privacy_conversation_fences(
 tenant_id uuid NOT NULL,conversation_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),
 policy_hash text NOT NULL CHECK(policy_hash ~ '^[a-f0-9]{{64}}$'),policy_revision bigint NOT NULL,
 source_digest text NOT NULL CHECK(source_digest ~ '^[a-f0-9]{{64}}$'),source_version bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,conversation_id),UNIQUE(tenant_id,actor_id,key),
 FOREIGN KEY(tenant_id,conversation_id,actor_id) REFERENCES {schema}.conversations(tenant_id,id,owner_principal_id),
 FOREIGN KEY(tenant_id,policy_revision) REFERENCES {schema}.privacy_effect_policies(tenant_id,revision)
);
ALTER TABLE {schema}.privacy_conversation_fences ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.privacy_conversation_fences FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.privacy_conversation_fences TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.privacy_conversation_fences FROM PUBLIC,{access_role};
CREATE TRIGGER privacy_fence_immutable BEFORE UPDATE OR DELETE ON {schema}.privacy_conversation_fences FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_fence_no_truncate BEFORE TRUNCATE ON {schema}.privacy_conversation_fences FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();

-- Lock the root at every owning content boundary, including checkpoint/native
-- worker writes. This also serializes a pre-existing writer against admission.
-- Usage settlement is deliberately outside this trigger: known bills must settle.
CREATE FUNCTION {schema}._privacy_conversation_growth_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE row_value jsonb;t uuid;cid uuid;rid uuid;
BEGIN
 row_value:=CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
 IF TG_OP='UPDATE' AND (to_jsonb(OLD)->'tenant_id' IS DISTINCT FROM to_jsonb(NEW)->'tenant_id' OR
  to_jsonb(OLD)->'conversation_id' IS DISTINCT FROM to_jsonb(NEW)->'conversation_id' OR
  to_jsonb(OLD)->'run_id' IS DISTINCT FROM to_jsonb(NEW)->'run_id') THEN
  RAISE EXCEPTION 'Content scope is immutable' USING ERRCODE='23514';END IF;
 t:=(row_value->>'tenant_id')::uuid;
 IF TG_TABLE_NAME='conversations' THEN cid:=(row_value->>'id')::uuid;
 ELSIF TG_TABLE_NAME IN ('checkpoints','checkpoint_writes','rag_attempts') THEN
  rid:=(row_value->>'run_id')::uuid;
  SELECT conversation_id INTO cid FROM {schema}.runs WHERE tenant_id=t AND id=rid;
 ELSE cid:=(row_value->>'conversation_id')::uuid;END IF;
 PERFORM id FROM {schema}.conversations WHERE tenant_id=t AND id=cid FOR UPDATE;
 IF EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences WHERE tenant_id=t AND conversation_id=cid) THEN
  RAISE EXCEPTION 'Conversation growth is fenced' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION {schema}._privacy_conversation_growth_guard() FROM PUBLIC,{access_role};
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.conversations FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.runs FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.messages FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.feedback FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.conversation_requests FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.assisted_requests FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.rag_citations FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.source_dependencies FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.checkpoints FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT OR UPDATE OR DELETE ON {schema}.checkpoint_writes FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();
CREATE TRIGGER privacy_growth_guard BEFORE INSERT ON {schema}.rag_attempts FOR EACH ROW EXECUTE FUNCTION {schema}._privacy_conversation_growth_guard();

CREATE FUNCTION {schema}.privacy_conversation_fence_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
DECLARE auth jsonb;i jsonb;t uuid;person uuid;target uuid;key_value uuid;writing boolean;c {schema}.conversations%ROWTYPE;
 prior {schema}.privacy_conversation_fences%ROWTYPE;hash text;policy jsonb;policy_result jsonb;inventory jsonb;deadline numeric;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('lock','fence','fence_status') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>4096 THEN RETURN jsonb_build_object('status','invalid');END IF;
 writing:=operation IN ('lock','fence');target:=(payload->>'id')::uuid;
 IF target IS NULL OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid');END IF;
 IF writing THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR NOT payload ?& ARRAY['id','key','csrf_hash','version','content_digest','policy_hash']
   OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR (payload->>'key')::uuid='00000000-0000-0000-0000-000000000000'::uuid
   OR jsonb_typeof(payload->'version') IS DISTINCT FROM 'number' OR (payload->>'version') !~ '^[0-9]+$' OR (payload->>'version')::numeric NOT BETWEEN 1 AND 999999999999999999
   OR jsonb_typeof(payload->'content_digest') IS DISTINCT FROM 'string' OR (payload->>'content_digest') !~ '^[a-f0-9]{{64}}$'
   OR jsonb_typeof(payload->'policy_hash') IS DISTINCT FROM 'string' OR (payload->>'policy_hash') !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid');END IF;
  key_value:=(payload->>'key')::uuid;
 ELSIF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF writing THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));
  PERFORM id FROM {schema}.principals WHERE id=person FOR UPDATE;
 END IF;
 auth:={schema}.authorization_call(token,'context',CASE WHEN writing THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=target AND owner_principal_id=person FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 IF operation='lock' THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 SELECT * INTO prior FROM {schema}.privacy_conversation_fences WHERE tenant_id=t AND conversation_id=target;
 IF NOT writing THEN
  RETURN jsonb_build_object('status','ok','id',target,'fenced',prior.conversation_id IS NOT NULL,'execution_state','held','erased',false,'reason',CASE WHEN prior.conversation_id IS NULL THEN 'not_requested' ELSE 'complete_store_proofs_required' END,'authority_expires_at',deadline);
 END IF;
 hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
 IF prior.conversation_id IS NOT NULL THEN
  IF prior.key IS DISTINCT FROM key_value OR prior.request_hash IS DISTINCT FROM hash THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN jsonb_build_object('status','ok','id',target,'key',prior.key,'fenced',true,'replayed',true,'execution_state','held','erased',false,'reason','complete_store_proofs_required','authority_expires_at',deadline);
 END IF;
 IF EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences WHERE tenant_id=t AND actor_id=person AND key=key_value) THEN RETURN jsonb_build_object('status','conflict');END IF;
 policy_result:={schema}.privacy_effect_policy_call(token,'effective','{{}}'::jsonb);
 IF policy_result->>'status' IS DISTINCT FROM 'ok' THEN RETURN policy_result;END IF;
 policy:=policy_result->'policy';
 IF encode(sha256(convert_to(policy::text,'UTF8')),'hex') IS DISTINCT FROM payload->>'policy_hash' THEN RETURN jsonb_build_object('status','conflict');END IF;
 inventory:={schema}.privacy_conversation_inventory(token,jsonb_build_object('id',target));
 IF inventory->>'status' IS DISTINCT FROM 'ok' THEN RETURN inventory;END IF;
 IF c.version IS DISTINCT FROM (payload->>'version')::bigint OR inventory->>'content_digest' IS DISTINCT FROM payload->>'content_digest' THEN RETURN jsonb_build_object('status','conflict');END IF;
 -- Close the admitted run and invalidate every existing writer epoch before the
 -- immutable fence is inserted. Financial/provider unknowns remain recorded.
 UPDATE {schema}.runs SET state='failed',writer_generation=writer_generation+1,version=version+1,lease_until=NULL WHERE tenant_id=t AND conversation_id=target AND state IN ('queued','running','waiting','held');
 UPDATE {schema}.conversations SET active_run_id=NULL,version=version+1,updated_at=clock_timestamp() WHERE tenant_id=t AND id=target;
 -- Recheck the finite authority/policy boundary after all waits and work.
 IF least(deadline,(policy->>'expires_at')::numeric)<=extract(epoch FROM clock_timestamp()) THEN RAISE EXCEPTION 'Privacy authority expired' USING ERRCODE='23514';END IF;
 INSERT INTO {schema}.privacy_conversation_fences(tenant_id,conversation_id,actor_id,key,request_hash,policy_hash,policy_revision,source_digest,source_version)
 VALUES(t,target,person,key_value,hash,payload->>'policy_hash',(policy->>'revision')::bigint,payload->>'content_digest',c.version);
 RETURN jsonb_build_object('status','ok','id',target,'key',key_value,'fenced',true,'replayed',false,'execution_state','held','erased',false,'reason','complete_store_proofs_required','authority_expires_at',least(deadline,(policy->>'expires_at')::numeric));
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_conversation_fence_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_conversation_fence_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='admin_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_policy_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_conversation_fence_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_effect_policy_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
                        AND NOT (f.proname='privacy_conversation_inventory' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, payload jsonb')
                        AND NOT (f.proname='workspace_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
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




-- Metadata hash is optimistic context, not execution permission. No v1 fallback.
CREATE OR REPLACE FUNCTION {schema}.privacy_conversation_inventory(token text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE auth jsonb;identity jsonb;t uuid;person uuid;target uuid;c {schema}.conversations%ROWTYPE;policy_result jsonb;policy jsonb;rule jsonb;
 effect_record jsonb;pack jsonb;counts jsonb;retained jsonb;latest timestamptz;deadline numeric;unknown_calls integer;dependencies integer;size_estimate bigint;copy_count bigint;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy inventory capability rejected';END IF;
 IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(payload))<>1 OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' THEN RETURN jsonb_build_object('status','invalid');END IF;
 target:=(payload->>'id')::uuid;
 auth:={schema}.authorization_call(token,'context','{{}}'::jsonb);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 identity:=auth->'identity';t:=(identity->>'active_tenant_id')::uuid;person:=(identity->>'principal_id')::uuid;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=target AND owner_principal_id=person FOR SHARE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 policy_result:={schema}.privacy_policy_call(token,'effective','{{}}'::jsonb);
 IF policy_result->>'status' IS DISTINCT FROM 'ok' THEN RETURN policy_result;END IF;
 policy:=policy_result->'policy';
 SELECT value INTO rule FROM jsonb_array_elements(policy->'rules') WHERE value->>'category'='conversations';
 IF rule IS NULL THEN RETURN jsonb_build_object('status','held');END IF;
 SELECT coalesce(sum(bytes),0),count(*) INTO size_estimate,copy_count FROM (
  SELECT octet_length(body)::bigint bytes FROM {schema}.messages WHERE tenant_id=t AND conversation_id=target UNION ALL
  SELECT octet_length(quote) FROM {schema}.rag_citations WHERE tenant_id=t AND conversation_id=target UNION ALL
  SELECT coalesce(octet_length(reason),0) FROM {schema}.feedback WHERE tenant_id=t AND conversation_id=target UNION ALL
  SELECT octet_length(response::text) FROM {schema}.conversation_requests WHERE tenant_id=t AND conversation_id=target UNION ALL
  SELECT octet_length(selection::text)+coalesce(octet_length(proposal::text),0) FROM {schema}.assisted_requests WHERE tenant_id=t AND conversation_id=target UNION ALL
  SELECT octet_length(p.state_payload::text)+octet_length(p.namespace) FROM {schema}.checkpoints p JOIN {schema}.runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id WHERE r.tenant_id=t AND r.conversation_id=target UNION ALL
  SELECT octet_length(p.value::text)+octet_length(p.namespace)+octet_length(p.task_path)+octet_length(p.task_id)+octet_length(p.channel) FROM {schema}.checkpoint_writes p JOIN {schema}.runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id WHERE r.tenant_id=t AND r.conversation_id=target) sizes;
 IF size_estimate>524288 OR copy_count>512 THEN RETURN jsonb_build_object('status','held','reason','paged_inventory_required');END IF;
 -- A single read projection defines content coverage. No full row or raw bytes
 -- are returned; material is hashed in the owning database only.
 pack:=jsonb_build_object(
  'name',c.manual_name,
  'messages',coalesce((SELECT jsonb_agg(jsonb_build_object('id',id,'body',body,'author',author_kind,'mode',provenance_mode) ORDER BY sequence) FROM {schema}.messages WHERE tenant_id=t AND conversation_id=target),'[]'::jsonb),
  'quotes',coalesce((SELECT jsonb_agg(jsonb_build_object('message_id',message_id,'label',label,'quote',quote) ORDER BY message_id,label) FROM {schema}.rag_citations WHERE tenant_id=t AND conversation_id=target),'[]'::jsonb),
  'feedback',coalesce((SELECT jsonb_agg(jsonb_build_object('message_id',message_id,'reason',reason) ORDER BY message_id) FROM {schema}.feedback WHERE tenant_id=t AND conversation_id=target),'[]'::jsonb),
  'responses',coalesce((SELECT jsonb_agg(jsonb_build_object('key',key,'response',response) ORDER BY key) FROM {schema}.conversation_requests WHERE tenant_id=t AND conversation_id=target),'[]'::jsonb),
  'assisted',coalesce((SELECT jsonb_agg(jsonb_build_object('run_id',run_id,'selection',selection,'proposal',proposal,'draft_id',draft_id) ORDER BY run_id) FROM {schema}.assisted_requests WHERE tenant_id=t AND conversation_id=target),'[]'::jsonb),
  'checkpoints',coalesce((SELECT jsonb_agg(jsonb_build_object('run_id',p.run_id,'namespace',p.namespace,'id',p.id,'state',p.state_payload) ORDER BY p.run_id,p.namespace,p.id) FROM {schema}.checkpoints p JOIN {schema}.runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id WHERE r.tenant_id=t AND r.conversation_id=target),'[]'::jsonb),
  'writes',coalesce((SELECT jsonb_agg(jsonb_build_object('run_id',p.run_id,'namespace',p.namespace,'checkpoint_id',p.checkpoint_id,'task',p.task_id,'path',p.task_path,'channel',p.channel,'index',p.write_index,'value',p.value) ORDER BY p.run_id,p.namespace,p.checkpoint_id,p.task_id,p.write_index) FROM {schema}.checkpoint_writes p JOIN {schema}.runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id WHERE r.tenant_id=t AND r.conversation_id=target),'[]'::jsonb));
 SELECT ep.policy INTO effect_record FROM {schema}.privacy_effect_policies ep WHERE ep.tenant_id=t ORDER BY ep.revision DESC LIMIT 1;
 IF effect_record IS NOT NULL AND (effect_record->>'expires_at')::numeric<=extract(epoch FROM clock_timestamp()) THEN effect_record:=NULL;END IF;
 counts:=jsonb_build_object('messages',jsonb_array_length(pack->'messages'),'quotes',jsonb_array_length(pack->'quotes'),'feedback',jsonb_array_length(pack->'feedback'),'cached_responses',jsonb_array_length(pack->'responses'),'assisted_copies',jsonb_array_length(pack->'assisted'),'checkpoints',jsonb_array_length(pack->'checkpoints'),'pending_writes',jsonb_array_length(pack->'writes'));
 SELECT max(moment) INTO latest FROM (
  SELECT c.created_at moment UNION ALL SELECT c.updated_at UNION ALL
  SELECT created_at FROM {schema}.messages WHERE tenant_id=t AND conversation_id=target UNION ALL
  SELECT updated_at FROM {schema}.feedback WHERE tenant_id=t AND conversation_id=target UNION ALL
  SELECT p.privacy_created_at FROM {schema}.checkpoints p JOIN {schema}.runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id WHERE r.tenant_id=t AND r.conversation_id=target UNION ALL
  SELECT p.privacy_created_at FROM {schema}.checkpoint_writes p JOIN {schema}.runs r ON r.tenant_id=p.tenant_id AND r.id=p.run_id WHERE r.tenant_id=t AND r.conversation_id=target) timestamps;
 SELECT count(*) INTO unknown_calls FROM {schema}.rag_attempts a JOIN {schema}.runs r ON r.tenant_id=a.tenant_id AND r.id=a.run_id WHERE r.tenant_id=t AND r.conversation_id=target AND a.state IN ('reserved','unknown');
 SELECT count(*) INTO dependencies FROM {schema}.assisted_requests WHERE tenant_id=t AND conversation_id=target AND draft_id IS NOT NULL;
 retained:=jsonb_build_object('source_business_records','not_owned_by_conversation','minimum_audit_metadata','separate_audit_retention; not anonymous','backup','unverified; held','provider','unverified; held');
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 RETURN jsonb_build_object('status','ok','tenant_id',t,'subject_id',target,'category','conversations','version',c.version,'policy_revision',policy->'revision',
  'effect_policy_hash',CASE WHEN effect_record IS NULL THEN NULL ELSE encode(sha256(convert_to(effect_record::text,'UTF8')),'hex') END,'policy_hash',encode(sha256(convert_to(policy::text,'UTF8')),'hex'),'content_digest',encode(sha256(convert_to(pack::text,'UTF8')),'hex'),'counts',counts,
  'content_projection','conversation-content-v1','eligible_after',extract(epoch FROM latest)+(rule->>'retention_seconds')::numeric,
  'unknown_provider_attempts',unknown_calls,'financial_dependencies',dependencies,'residuals',retained,'execution_state','held','erased',false,'authority_expires_at',deadline);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_conversation_inventory(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_conversation_inventory(text,jsonb) TO {access_role};

