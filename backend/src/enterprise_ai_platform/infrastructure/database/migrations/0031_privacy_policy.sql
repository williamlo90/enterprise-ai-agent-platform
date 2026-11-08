-- DATA01: policy configuration only. No content-read or erase capability.
CREATE TABLE {schema}.privacy_policies(
 tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),revision bigint NOT NULL CHECK(revision>0),
 policy jsonb NOT NULL CHECK(jsonb_typeof(policy)='object'),approved_by uuid NOT NULL REFERENCES {schema}.principals(id),
 approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(tenant_id,revision)
);
CREATE TABLE {schema}.privacy_policy_decisions(
 tenant_id uuid NOT NULL,actor_id uuid NOT NULL REFERENCES {schema}.principals(id),key uuid NOT NULL,
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),revision bigint NOT NULL,response jsonb NOT NULL,
 PRIMARY KEY(tenant_id,actor_id,key),FOREIGN KEY(tenant_id,revision) REFERENCES {schema}.privacy_policies(tenant_id,revision)
);
ALTER TABLE {schema}.privacy_policies ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_policies FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.privacy_policy_decisions ENABLE ROW LEVEL SECURITY;ALTER TABLE {schema}.privacy_policy_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY maintenance ON {schema}.privacy_policies TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY maintenance ON {schema}.privacy_policy_decisions TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.privacy_policies,{schema}.privacy_policy_decisions FROM PUBLIC,{access_role};
CREATE TRIGGER privacy_policy_immutable BEFORE UPDATE OR DELETE ON {schema}.privacy_policies FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_policy_no_truncate BEFORE TRUNCATE ON {schema}.privacy_policies FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_decision_immutable BEFORE UPDATE OR DELETE ON {schema}.privacy_policy_decisions FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER privacy_decision_no_truncate BEFORE TRUNCATE ON {schema}.privacy_policy_decisions FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();

CREATE FUNCTION {schema}._privacy_rules_valid(rules jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE r jsonb;required text[];actual text[];
BEGIN
 IF jsonb_typeof(rules) IS DISTINCT FROM 'array' OR jsonb_array_length(rules)<>9 THEN RETURN false;END IF;
 IF (SELECT count(DISTINCT value->>'category') FROM jsonb_array_elements(rules))<>9 THEN RETURN false;END IF;
 FOR r IN SELECT value FROM jsonb_array_elements(rules) LOOP
  IF jsonb_typeof(r) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(r))<>6
   OR NOT r ?& ARRAY['category','retention_seconds','regions','stores','provider_retention_seconds','training_allowed']
   OR jsonb_typeof(r->'category') IS DISTINCT FROM 'string' OR r->'training_allowed' IS DISTINCT FROM 'false'::jsonb
   OR jsonb_typeof(r->'retention_seconds') IS DISTINCT FROM 'number' OR (r->>'retention_seconds') !~ '^[0-9]+$'
   OR (r->>'retention_seconds')::numeric NOT BETWEEN 1 AND 3155760000
   OR jsonb_typeof(r->'provider_retention_seconds') IS DISTINCT FROM 'number' OR (r->>'provider_retention_seconds') !~ '^[0-9]+$'
   OR (r->>'provider_retention_seconds')::numeric NOT BETWEEN 0 AND (r->>'retention_seconds')::numeric THEN RETURN false;END IF;
  required:=CASE r->>'category'
   WHEN 'identity' THEN ARRAY['backup','cache','db','ledger','provider'] WHEN 'knowledge' THEN ARRAY['backup','cache','db','file','index','provider']
   WHEN 'conversations' THEN ARRAY['backup','cache','checkpoint','db','provider'] WHEN 'drafts' THEN ARRAY['backup','cache','checkpoint','db','ledger']
   WHEN 'receipts' THEN ARRAY['backup','db','file','ledger','pins'] WHEN 'claims' THEN ARRAY['backup','db','ledger']
   WHEN 'audit' THEN ARRAY['backup','db'] WHEN 'recovery' THEN ARRAY['backup','db','ledger'] WHEN 'model' THEN ARRAY['backup','cache','provider'] ELSE NULL END;
  IF required IS NULL OR jsonb_typeof(r->'stores') IS DISTINCT FROM 'array' OR jsonb_array_length(r->'stores')<>cardinality(required)
   OR EXISTS(SELECT 1 FROM jsonb_array_elements(r->'stores') WHERE jsonb_typeof(value)<>'string') THEN RETURN false;END IF;
  SELECT array_agg(value ORDER BY value) INTO actual FROM jsonb_array_elements_text(r->'stores');
  IF actual IS DISTINCT FROM required THEN RETURN false;END IF;
  IF jsonb_typeof(r->'regions') IS DISTINCT FROM 'array' OR jsonb_array_length(r->'regions') NOT BETWEEN 1 AND 10
   OR EXISTS(SELECT 1 FROM jsonb_array_elements(r->'regions') WHERE jsonb_typeof(value)<>'string' OR value#>>'{{}}' !~ '^[A-Za-z0-9-]{{1,64}}$')
   OR (SELECT count(DISTINCT value) FROM jsonb_array_elements(r->'regions'))<>jsonb_array_length(r->'regions') THEN RETURN false;END IF;
 END LOOP;
 RETURN true;
EXCEPTION WHEN data_exception THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION {schema}._privacy_rules_valid(jsonb) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.privacy_policy_call(token text,operation text,payload jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
#variable_conflict use_column
<<privacy>>
DECLARE auth jsonb;i jsonb;t uuid;person uuid;key uuid;deadline numeric;hash text;latest bigint;record jsonb;result jsonb;moment timestamptz;prior {schema}.privacy_policy_decisions%ROWTYPE;writing boolean;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Privacy capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('lock','current','effective','publish') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR octet_length(payload::text)>65536 THEN RETURN jsonb_build_object('status','invalid');END IF;
 writing:=operation IN ('lock','publish');
 SELECT principal_id,active_tenant_id INTO person,t FROM {schema}.sessions WHERE token_hash=token;
 IF person IS NULL OR t IS NULL THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 IF writing THEN
  IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['key','csrf_hash','revision','expires_at','rules']
   OR jsonb_typeof(payload->'key') IS DISTINCT FROM 'string' OR (payload->>'key')::uuid='00000000-0000-0000-0000-000000000000'::uuid
   OR jsonb_typeof(payload->'revision') IS DISTINCT FROM 'number' OR (payload->>'revision') !~ '^[0-9]+$' OR (payload->>'revision')::numeric NOT BETWEEN 1 AND 999999999999999999
   OR jsonb_typeof(payload->'expires_at') IS DISTINCT FROM 'number' OR (payload->>'expires_at')::numeric>253402300799
   OR (payload->>'expires_at')::numeric<=extract(epoch FROM clock_timestamp()) OR NOT {schema}._privacy_rules_valid(payload->'rules') THEN RETURN jsonb_build_object('status','invalid');END IF;
  key:=(payload->>'key')::uuid;
  PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||t::text,0));
  PERFORM id FROM {schema}.principals WHERE id=person FOR UPDATE;
 ELSIF payload<>'{{}}'::jsonb THEN RETURN jsonb_build_object('status','invalid');END IF;
 auth:={schema}.authorization_call(token,'context',CASE WHEN writing THEN jsonb_build_object('csrf_hash',payload->>'csrf_hash') ELSE '{{}}'::jsonb END);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';
 IF i->>'active_tenant_id' IS DISTINCT FROM t::text OR i->>'principal_id' IS DISTINCT FROM person::text THEN RETURN jsonb_build_object('status','conflict');END IF;
 IF operation<>'effective' AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE g->>'role'='operator_scoped' AND g->>'scope_kind'='operations' AND g->>'scope_ref'='config') THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT extract(epoch FROM least(expires_at,last_seen_at+interval '1800 seconds')) INTO deadline FROM {schema}.sessions WHERE token_hash=token;
 IF operation='lock' THEN RETURN jsonb_build_object('status','ok','authority_expires_at',deadline);END IF;
 IF NOT writing THEN
  SELECT policy,revision INTO record,latest FROM {schema}.privacy_policies WHERE tenant_id=t ORDER BY revision DESC LIMIT 1;
  IF operation='effective' AND (record IS NULL OR (record->>'expires_at')::numeric<=extract(epoch FROM clock_timestamp())) THEN RETURN jsonb_build_object('status','held');END IF;
  RETURN jsonb_build_object('status','ok','policy',record,'revision',coalesce(latest,0),'authority_expires_at',deadline);
 END IF;
 hash:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
 SELECT * INTO prior FROM {schema}.privacy_policy_decisions WHERE tenant_id=t AND actor_id=person AND privacy_policy_decisions.key=privacy.key;
 IF FOUND THEN
  IF prior.request_hash<>hash THEN RETURN jsonb_build_object('status','conflict');END IF;
  RETURN prior.response||jsonb_build_object('status','ok','replayed',true,'authority_expires_at',deadline);
 END IF;
 SELECT coalesce(max(revision),0) INTO latest FROM {schema}.privacy_policies WHERE tenant_id=t;
 IF (payload->>'revision')::bigint<>latest+1 THEN RETURN jsonb_build_object('status','conflict');END IF;
 moment:=clock_timestamp();
 record:=jsonb_build_object('tenant_id',t,'revision',latest+1,'approved_by',person,'approved_at',extract(epoch FROM moment),'expires_at',payload->'expires_at','rules',payload->'rules');
 result:=jsonb_build_object('policy',record,'revision',latest+1,'receipt',jsonb_build_object('key',key,'request_hash',hash,'policy_hash',encode(sha256(convert_to(record::text,'UTF8')),'hex')));
 INSERT INTO {schema}.privacy_policies(tenant_id,revision,policy,approved_by,approved_at) VALUES(t,latest+1,record,person,moment);
 INSERT INTO {schema}.privacy_policy_decisions(tenant_id,actor_id,key,request_hash,revision,response) VALUES(t,person,key,hash,latest+1,result);
 RETURN result||jsonb_build_object('status','ok','replayed',false,'authority_expires_at',deadline);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.privacy_policy_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.privacy_policy_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('privacy_policies','privacy_policy_decisions','registered_config_refs','admin_requests','admin_events','assisted_requests','action_claim_bindings','flow_results','actions','human_action_receipts','consents','action_audit','drafts','draft_versions','draft_requests','draft_previews','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



