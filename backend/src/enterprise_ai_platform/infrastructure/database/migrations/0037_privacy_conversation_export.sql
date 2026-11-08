-- DATA01: response-only export; raw archives never enter a table/cache/object.
-- Physical location facts are enrolled by deployment operators, never a browser
-- or the approving model. Empty/unverified enrollment keeps exports held.
CREATE TABLE {schema}.privacy_store_locations(
 runtime_role oid NOT NULL,store text NOT NULL CHECK(store IN ('db','cache')),region text NOT NULL,
 verified_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,evidence_sha256 text NOT NULL CHECK(evidence_sha256 ~ '^[a-f0-9]{{64}}$'),
 environment text NOT NULL CHECK(environment IN ('production','fictional_fixture')),PRIMARY KEY(runtime_role,store),CHECK(expires_at>verified_at)
);
CREATE TABLE {schema}.privacy_export_receipts(
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL,key uuid NOT NULL,conversation_id uuid NOT NULL,
 session_id uuid NOT NULL REFERENCES {schema}.sessions(id),membership_revision bigint NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),policy_revision bigint NOT NULL,
 content_digest text NOT NULL CHECK(content_digest ~ '^[a-f0-9]{{64}}$'),
 created_at timestamptz NOT NULL,expires_at timestamptz NOT NULL,metadata_expires_at timestamptz NOT NULL,
 PRIMARY KEY(tenant_id,actor_id,key),FOREIGN KEY(tenant_id,conversation_id,actor_id) REFERENCES {schema}.conversations(tenant_id,id,owner_principal_id),
 FOREIGN KEY(tenant_id,policy_revision) REFERENCES {schema}.privacy_effect_policies(tenant_id,revision),CHECK(expires_at>created_at),CHECK(metadata_expires_at>created_at)
);
ALTER TABLE {schema}.privacy_store_locations ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_store_locations FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.privacy_export_receipts ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_export_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.privacy_store_locations TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.privacy_export_receipts TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.privacy_store_locations,{schema}.privacy_export_receipts FROM PUBLIC,{access_role};
CREATE TRIGGER privacy_export_immutable BEFORE UPDATE OR DELETE ON {schema}.privacy_export_receipts FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_export_no_truncate BEFORE TRUNCATE ON {schema}.privacy_export_receipts FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();

CREATE FUNCTION {schema}.privacy_conversation_export_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
DECLARE person uuid;t uuid;target uuid;request_key uuid;auth jsonb;i jsonb;deadline numeric;c {schema}.conversations%ROWTYPE;
 p jsonb;approved jsonb;inventory jsonb;rule jsonb;audit_rule jsonb;location {schema}.privacy_store_locations%ROWTYPE;
 db_location {schema}.privacy_store_locations%ROWTYPE;prior {schema}.privacy_export_receipts%ROWTYPE;
 m {schema}.messages%ROWTYPE;item jsonb;refs jsonb;decision jsonb;allowed boolean;items jsonb:='[]'::jsonb;content jsonb;
 created timestamptz;expiry timestamptz;metadata_expiry timestamptz;hash text;content_hash text;s {schema}.sessions%ROWTYPE;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('lock','export') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>4096
  OR (SELECT count(*) FROM jsonb_object_keys(payload))<>6 OR NOT payload ?& ARRAY['id','key','csrf_hash','version','content_digest','policy_hash']
  OR jsonb_typeof(payload->'id') IS DISTINCT FROM 'string' OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string'
  OR jsonb_typeof(payload->'version') IS DISTINCT FROM 'number' OR (payload->>'version') !~ '^[0-9]+$' OR (payload->>'version')::numeric NOT BETWEEN 1 AND 999999999999999999
  OR jsonb_typeof(payload->'content_digest') IS DISTINCT FROM 'string' OR (payload->>'content_digest') !~ '^[a-f0-9]{{64}}$'
  OR jsonb_typeof(payload->'policy_hash') IS DISTINCT FROM 'string' OR (payload->>'policy_hash') !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid');END IF;
 target:=(payload->>'id')::uuid;request_key:=(payload->>'key')::uuid;
 IF request_key='00000000-0000-0000-0000-000000000000'::uuid THEN RETURN jsonb_build_object('status','invalid');END IF;
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));
 PERFORM id FROM {schema}.principals WHERE id=person FOR UPDATE;
 auth:={schema}.authorization_call(token,'context',jsonb_build_object('csrf_hash',payload->>'csrf_hash'));
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO c FROM {schema}.conversations WHERE tenant_id=t AND id=target AND owner_principal_id=person FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE token_hash=token;
 deadline:=extract(epoch FROM least(s.expires_at,s.last_seen_at+interval '1800 seconds'));
 IF operation='lock' THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 approved:={schema}.privacy_effect_policy_call(token,'effective','{{}}'::jsonb);
 IF approved->>'status' IS DISTINCT FROM 'ok' THEN RETURN approved;END IF;p:=approved->'policy';
 IF encode(sha256(convert_to(p::text,'UTF8')),'hex') IS DISTINCT FROM payload->>'policy_hash' THEN RETURN jsonb_build_object('status','conflict');END IF;
 inventory:={schema}.privacy_conversation_inventory(token,jsonb_build_object('id',target));
 IF inventory->>'status' IS DISTINCT FROM 'ok' THEN RETURN inventory;END IF;
 IF c.version IS DISTINCT FROM (payload->>'version')::bigint OR inventory->>'content_digest' IS DISTINCT FROM payload->>'content_digest' THEN RETURN jsonb_build_object('status','conflict');END IF;
 SELECT value INTO rule FROM jsonb_array_elements(p->'copy_rules') WHERE value->>'category'='conversations' AND value->>'store'='cache' AND value->>'copy_class'='export_artifact';
 SELECT value INTO audit_rule FROM jsonb_array_elements(p->'copy_rules') WHERE value->>'category'='audit' AND value->>'store'='db' AND value->>'copy_class'='audit_metadata';
 SELECT * INTO location FROM {schema}.privacy_store_locations WHERE runtime_role=session_user::regrole::oid AND store='cache' AND verified_at<=clock_timestamp() AND expires_at>clock_timestamp();
 SELECT * INTO db_location FROM {schema}.privacy_store_locations WHERE runtime_role=session_user::regrole::oid AND store='db' AND verified_at<=clock_timestamp() AND expires_at>clock_timestamp();
 IF rule IS NULL OR rule->>'min_retain_seconds' NOT IN ('0','not_applicable') OR audit_rule IS NULL OR location.region IS NULL OR db_location.region IS NULL
  OR NOT (rule->'regions') ? location.region OR NOT (audit_rule->'regions') ? db_location.region
  OR (location.environment='fictional_fixture' OR db_location.environment='fictional_fixture') AND (SELECT n.nspname FROM pg_catalog.pg_namespace n WHERE n.oid=(SELECT c.relnamespace FROM pg_catalog.pg_class c WHERE c.oid='{schema}.privacy_store_locations'::regclass)) !~ '^db03_probe_[a-f0-9]+$'
 THEN RETURN jsonb_build_object('status','held');END IF;
 hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
 SELECT * INTO prior FROM {schema}.privacy_export_receipts WHERE tenant_id=t AND actor_id=person AND key=request_key;
 IF FOUND THEN
  IF prior.request_hash IS DISTINCT FROM hash OR prior.session_id IS DISTINCT FROM s.id OR prior.membership_revision IS DISTINCT FROM s.membership_revision THEN RETURN jsonb_build_object('status','conflict');END IF;
  IF clock_timestamp()>=prior.expires_at THEN RETURN jsonb_build_object('status','expired');END IF;
  created:=prior.created_at;expiry:=prior.expires_at;metadata_expiry:=prior.metadata_expires_at;
 ELSE
  created:=clock_timestamp();expiry:=least(created+make_interval(secs=>(rule->>'expire_after_seconds')::double precision),to_timestamp((p->>'expires_at')::double precision),to_timestamp(deadline::double precision),location.expires_at,db_location.expires_at);
  metadata_expiry:=created+make_interval(secs=>(audit_rule->>'expire_after_seconds')::double precision);
 END IF;
 FOR m IN SELECT * FROM {schema}.messages WHERE tenant_id=t AND conversation_id=target ORDER BY sequence LOOP
  item:={schema}._conversation_message(m);refs:=item->'_sources';allowed:=false;
  IF m.author_kind='human' THEN allowed:=true;
  ELSIF m.provenance_mode='source_free' AND jsonb_array_length(refs)=0 AND jsonb_array_length(item->'citations')=0 THEN allowed:=true;
  ELSIF m.provenance_mode='dependent' AND jsonb_array_length(refs)>0 THEN
   decision:={schema}.document_sources_call(jsonb_build_object('token',token),refs,true);
   IF decision->>'status' IS DISTINCT FROM 'ok' THEN RETURN decision;END IF;
   -- Service references remain false without an authenticated current owning
   -- service proof. Never combine the advisory service_eligible flag as access.
   allowed:=jsonb_array_length(decision->'decisions')=jsonb_array_length(refs) AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(decision->'decisions') v WHERE v.value IS DISTINCT FROM 'true'::jsonb);
  END IF;
  items:=items||jsonb_build_array(CASE WHEN allowed THEN jsonb_build_object('sequence',m.sequence,'author_kind',m.author_kind,'body',m.body,'created_at',m.created_at,'feedback',item->'feedback','citations',CASE WHEN m.author_kind<>'human' AND jsonb_array_length(refs)>0 THEN item->'citations' ELSE '[]'::jsonb END) ELSE jsonb_build_object('sequence',m.sequence,'author_kind',m.author_kind,'redacted',true,'reason','current_source_unavailable') END);
 END LOOP;
 content:=jsonb_build_object('schema_version',1,'conversation',jsonb_build_object('id',target,'language',c.language,'name',c.manual_name),'messages',items,'scope','current-authorized-conversation-content; runtime state and credentials excluded');
 IF octet_length(content::text)>524288 THEN RETURN jsonb_build_object('status','held');END IF;
 content_hash:=encode(sha256(convert_to(content::text,'UTF8')),'hex');
 IF prior.key IS NOT NULL AND prior.content_digest IS DISTINCT FROM content_hash THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF extract(epoch FROM clock_timestamp())>=least(deadline,(p->>'expires_at')::numeric,extract(epoch FROM expiry)) THEN RETURN jsonb_build_object('status','expired');END IF;
 IF prior.key IS NULL THEN
  INSERT INTO {schema}.privacy_export_receipts(tenant_id,actor_id,key,conversation_id,session_id,membership_revision,request_hash,policy_revision,content_digest,created_at,expires_at,metadata_expires_at)
  VALUES(t,person,request_key,target,s.id,s.membership_revision,hash,(p->>'revision')::bigint,content_hash,created,expiry,metadata_expiry);
 END IF;
 RETURN jsonb_build_object('status','ok','document',jsonb_build_object('generated_at',created,'expires_at',expiry,'storage','response_only','content',content),'authority_expires_at',least(deadline,(p->>'expires_at')::numeric,extract(epoch FROM expiry)));
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_conversation_export_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_conversation_export_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','privacy_store_locations','privacy_export_receipts','privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('privacy_store_locations','privacy_export_receipts','privacy_fence_releases','privacy_conversation_effect_intents','privacy_conversation_fences','privacy_effect_policies','privacy_effect_decisions','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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
                        AND NOT (f.proname='privacy_conversation_export_call' AND pg_catalog.pg_get_function_identity_arguments(f.oid)='token text, operation text, payload jsonb')
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




