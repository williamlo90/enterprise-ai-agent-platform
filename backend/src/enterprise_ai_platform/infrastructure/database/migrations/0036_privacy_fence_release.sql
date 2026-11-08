-- DATA01: recorded cancellation never revives a prior run epoch.
-- Earlier fence-only rows have no asynchronous effect authority; never backfill
-- a newer session as the original approval. Such rows remain executor-held.
ALTER TABLE {schema}.privacy_conversation_fences ADD COLUMN original_session_id uuid REFERENCES {schema}.sessions(id),
 ADD COLUMN original_membership_id uuid,ADD COLUMN original_access_revision bigint CHECK(original_access_revision>0),
 ADD FOREIGN KEY(tenant_id,original_membership_id,actor_id) REFERENCES {schema}.memberships(tenant_id,id,principal_id);
ALTER TABLE {schema}.privacy_conversation_fences DROP CONSTRAINT privacy_conversation_fences_pkey;
ALTER TABLE {schema}.privacy_conversation_fences ADD PRIMARY KEY(tenant_id,actor_id,key);
CREATE INDEX privacy_fences_by_root ON {schema}.privacy_conversation_fences(tenant_id,conversation_id,created_at DESC,key DESC);
CREATE TABLE {schema}.privacy_fence_releases(
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,fence_key uuid NOT NULL,conversation_id uuid NOT NULL,
 released_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,actor_id,key),UNIQUE(tenant_id,actor_id,fence_key),
 FOREIGN KEY(tenant_id,actor_id,fence_key) REFERENCES {schema}.privacy_conversation_fences(tenant_id,actor_id,key)
);
-- A future owning executor must reserve this guard under the same root lock
-- before an irreversible effect. Pending/unknown intent cannot be cancelled.
CREATE TABLE {schema}.privacy_conversation_effect_intents(
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,fence_key uuid NOT NULL,effect_key uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,actor_id,fence_key),
 FOREIGN KEY(tenant_id,actor_id,fence_key) REFERENCES {schema}.privacy_conversation_fences(tenant_id,actor_id,key)
);
ALTER TABLE {schema}.privacy_fence_releases ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_fence_releases FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.privacy_conversation_effect_intents ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_conversation_effect_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.privacy_fence_releases TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.privacy_conversation_effect_intents TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.privacy_fence_releases,{schema}.privacy_conversation_effect_intents FROM PUBLIC,{access_role};
CREATE TRIGGER privacy_release_immutable BEFORE UPDATE OR DELETE ON {schema}.privacy_fence_releases FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_release_no_truncate BEFORE TRUNCATE ON {schema}.privacy_fence_releases FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_effect_intent_immutable BEFORE UPDATE OR DELETE ON {schema}.privacy_conversation_effect_intents FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_effect_intent_no_truncate BEFORE TRUNCATE ON {schema}.privacy_conversation_effect_intents FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();

CREATE FUNCTION {schema}.privacy_conversation_release_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
DECLARE person uuid;t uuid;target uuid;request_key uuid;target_key uuid;auth jsonb;i jsonb;deadline numeric;
 f {schema}.privacy_conversation_fences%ROWTYPE;prior {schema}.privacy_fence_releases%ROWTYPE;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('lock','release') OR jsonb_typeof(payload) IS DISTINCT FROM 'object'
  OR (SELECT count(*) FROM jsonb_object_keys(payload))<>4 OR NOT payload ?& ARRAY['id','key','fence_key','csrf_hash']
  OR EXISTS(SELECT 1 FROM jsonb_each(payload) WHERE jsonb_typeof(value)<>'string') THEN RETURN jsonb_build_object('status','invalid');END IF;
 target:=(payload->>'id')::uuid;request_key:=(payload->>'key')::uuid;target_key:=(payload->>'fence_key')::uuid;
 IF request_key='00000000-0000-0000-0000-000000000000'::uuid OR target_key='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));
 PERFORM id FROM {schema}.principals WHERE id=person FOR UPDATE;
 auth:={schema}.authorization_call(token,'context',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 PERFORM id FROM {schema}.conversations WHERE tenant_id=t AND id=target AND owner_principal_id=person FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 IF operation='lock' THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 SELECT * INTO f FROM {schema}.privacy_conversation_fences WHERE tenant_id=t AND actor_id=person AND key=target_key AND conversation_id=target;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT * INTO prior FROM {schema}.privacy_fence_releases WHERE tenant_id=t AND actor_id=person AND key=request_key;
 IF FOUND THEN
  IF prior.fence_key IS DISTINCT FROM target_key OR prior.conversation_id IS DISTINCT FROM target THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN jsonb_build_object('status','ok','id',target,'fence_key',target_key,'released',true,'replayed',true,'erased',false,'authority_expires_at',deadline);
 END IF;
 IF EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases WHERE tenant_id=t AND actor_id=person AND fence_key=target_key)
  OR EXISTS(SELECT 1 FROM {schema}.privacy_conversation_effect_intents WHERE tenant_id=t AND actor_id=person AND fence_key=target_key) THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF deadline<=extract(epoch FROM clock_timestamp()) THEN RAISE EXCEPTION 'Privacy authority expired' USING ERRCODE='23514';END IF;
 INSERT INTO {schema}.privacy_fence_releases(tenant_id,actor_id,key,fence_key,conversation_id) VALUES(t,person,request_key,target_key,target);
 RETURN jsonb_build_object('status','ok','id',target,'fence_key',target_key,'released',true,'replayed',false,'erased',false,'authority_expires_at',deadline);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_conversation_release_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_conversation_release_call(text,text,jsonb) TO {access_role};

CREATE OR REPLACE FUNCTION {schema}._privacy_conversation_growth_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
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
 IF EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.conversation_id=cid AND NOT EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases e WHERE e.tenant_id=f.tenant_id AND e.actor_id=f.actor_id AND e.fence_key=f.key)) THEN
  RAISE EXCEPTION 'Conversation growth is fenced' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD;END IF;RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION {schema}._privacy_conversation_growth_guard() FROM PUBLIC,{access_role};

CREATE OR REPLACE FUNCTION {schema}.privacy_conversation_fence_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
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
 SELECT * INTO prior FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.conversation_id=target AND NOT EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases e WHERE e.tenant_id=f.tenant_id AND e.actor_id=f.actor_id AND e.fence_key=f.key) ORDER BY f.created_at DESC,f.key DESC LIMIT 1;
 IF NOT writing THEN
  RETURN jsonb_build_object('status','ok','id',target,'fenced',prior.conversation_id IS NOT NULL,'execution_state','held','erased',false,'reason',CASE WHEN prior.conversation_id IS NULL THEN 'not_requested' ELSE 'complete_store_proofs_required' END,'authority_expires_at',deadline);
 END IF;
 hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
 -- A released request remains replayable as released, never rearms old writers.
 IF EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.actor_id=person AND f.key=key_value
  AND EXISTS(SELECT 1 FROM {schema}.privacy_fence_releases e WHERE e.tenant_id=t AND e.actor_id=person AND e.fence_key=f.key)) THEN
  IF NOT EXISTS(SELECT 1 FROM {schema}.privacy_conversation_fences f WHERE f.tenant_id=t AND f.actor_id=person AND f.key=key_value AND f.conversation_id=target AND f.request_hash=hash) THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN jsonb_build_object('status','ok','id',target,'key',key_value,'fenced',prior.conversation_id IS NOT NULL,'released',true,'replayed',true,'execution_state','cancelled','erased',false,'authority_expires_at',deadline);
 END IF;
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
 INSERT INTO {schema}.privacy_conversation_fences(tenant_id,conversation_id,actor_id,key,request_hash,policy_hash,policy_revision,source_digest,source_version,original_session_id,original_membership_id,original_access_revision)
 VALUES(t,target,person,key_value,hash,payload->>'policy_hash',(policy->>'revision')::bigint,payload->>'content_digest',c.version,(SELECT id FROM {schema}.sessions WHERE token_hash=token),(i->>'active_membership_id')::uuid,(i->>'access_revision')::bigint);
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='privacy_conversation_release_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
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




