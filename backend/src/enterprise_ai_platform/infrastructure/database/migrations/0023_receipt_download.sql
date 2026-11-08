-- Current claim-scoped receipt reads. No role-only or browser supplied ACL.
CREATE FUNCTION {schema}.receipt_file_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE auth jsonb;identity jsonb;context jsonb;envelope jsonb;material jsonb;access jsonb;ref jsonb;
 s {schema}.sessions%ROWTYPE;f {schema}.files%ROWTYPE;k {schema}.receipt_receiver_keys%ROWTYPE;
 caller {schema}.internal_caller_credentials%ROWTYPE;deadline numeric;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Receipt file capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('context','reference','download') OR jsonb_typeof(payload) IS DISTINCT FROM 'object'
  OR octet_length(payload::text)>16384 THEN RETURN jsonb_build_object('status','invalid');END IF;
 auth:={schema}.authorization_call(token,'context','{{}}'::jsonb);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 identity:=auth->'identity';
 SELECT * INTO s FROM {schema}.sessions WHERE token_hash=token FOR SHARE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 context:=jsonb_build_object('tenant_id',identity->>'active_tenant_id','actor_id',identity->>'principal_id',
  'membership_id',identity->>'active_membership_id','session_id',s.id,'access_revision',identity->'access_revision');
 deadline:=extract(epoch FROM least(s.expires_at,s.last_seen_at+interval '1800 seconds'));
 IF operation='context' THEN
  IF payload<>'{{}}'::jsonb THEN RETURN jsonb_build_object('status','invalid');END IF;
  IF extract(epoch FROM clock_timestamp())>=deadline THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
  RETURN jsonb_build_object('status','ok','context',context||jsonb_build_object('authority_expires_at',deadline),'authority_expires_at',deadline);
 END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['id','claim_id','attestation']
  THEN RETURN jsonb_build_object('status','invalid');END IF;
 envelope:=payload->'attestation';material:=envelope->'material';access:=material->'access';
 IF jsonb_typeof(envelope) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(envelope))<>4
  OR NOT envelope ?& ARRAY['credential_hash','key_id','material','proof'] OR envelope->>'proof' !~ '^[a-f0-9]{{64}}$'
  OR jsonb_typeof(material) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(material))<>3
  OR NOT material ?& ARRAY['schema_version','context','access'] OR material->'schema_version' IS DISTINCT FROM '1'::jsonb
  OR material->'context' IS DISTINCT FROM context
  OR jsonb_typeof(access) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(access))<>7
  OR NOT access ?& ARRAY['tenant_id','actor_id','claim_id','claim_version','revision','receipts','expires_at']
  OR access->>'tenant_id' IS DISTINCT FROM context->>'tenant_id' OR access->>'actor_id' IS DISTINCT FROM context->>'actor_id'
  OR access->>'claim_id' IS DISTINCT FROM payload->>'claim_id'
  OR jsonb_typeof(access->'claim_version') IS DISTINCT FROM 'number' OR access->>'claim_version' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(access->'revision') IS DISTINCT FROM 'number' OR access->>'revision' !~ '^[1-9][0-9]*$'
  OR jsonb_typeof(access->'expires_at') IS DISTINCT FROM 'number' OR access->>'expires_at' !~ '^[1-9][0-9]*$'
  OR (access->>'expires_at')::numeric>extract(epoch FROM clock_timestamp())+65
  OR jsonb_typeof(access->'receipts') IS DISTINCT FROM 'array' OR jsonb_array_length(access->'receipts')>10
  THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO caller FROM {schema}.internal_caller_credentials WHERE credential_hash=envelope->>'credential_hash' FOR SHARE;
 IF NOT FOUND OR NOT caller.active OR caller.audience<>'reference-reimbursement' OR caller.delegation_caller<>'platform-dispatcher'
  OR NOT caller.allowed_actions ? 'claim.read' THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO k FROM {schema}.receipt_receiver_keys WHERE credential_hash=envelope->>'credential_hash' AND key_id=envelope->>'key_id' FOR SHARE;
 IF NOT FOUND OR NOT k.active OR clock_timestamp()<k.not_before OR clock_timestamp()>=k.not_after
  OR envelope->>'proof' IS DISTINCT FROM encode({schema}._policy_hmac(k.key_material,
    convert_to('eai:receipt-download:v1:'||{schema}._policy_canonical(material),'UTF8')),'hex')
  THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO f FROM {schema}.files WHERE tenant_id=(context->>'tenant_id')::uuid AND id=(payload->>'id')::uuid FOR SHARE;
 IF NOT FOUND OR f.purpose<>'receipt' OR f.scan_state<>'ready' OR f.deletion_state NOT IN ('active','retained') OR f.object_version IS NULL
  THEN RETURN jsonb_build_object('status','missing');END IF;
 SELECT value INTO ref FROM jsonb_array_elements(access->'receipts') WHERE value->>'id'=f.id::text;
 IF ref IS NULL OR ref<>jsonb_build_object('id',f.id,'version',f.version,'digest',f.digest)
  OR NOT EXISTS(SELECT 1 FROM {schema}.receipt_pins p JOIN {schema}.receipt_pin_batches b USING(tenant_id,operation_id)
    WHERE p.tenant_id=f.tenant_id AND p.file_id=f.id AND p.file_version=f.version AND p.file_digest=f.digest
     AND b.candidate_claim_id=(payload->>'claim_id')::uuid AND b.state IN ('held','finalised'))
  THEN RETURN jsonb_build_object('status','missing');END IF;
 auth:={schema}.authorization_call(token,'finish','{{}}'::jsonb);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 deadline:=least(deadline,(access->>'expires_at')::numeric,extract(epoch FROM k.not_after),extract(epoch FROM caller.expires_at));
 IF extract(epoch FROM clock_timestamp())>=deadline THEN RETURN jsonb_build_object('status','unauthenticated');END IF;
 RETURN {schema}._file_json(f)||jsonb_build_object('authority_expires_at',deadline);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.receipt_file_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.receipt_file_call(text,text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('receipt_receiver_keys','receipt_pin_batches','receipt_pins','delegation_verification_contexts','policy_packages','policy_document_refs','policy_coordination','policy_windows','policy_requests','policy_dispatches','policy_audit','policy_receiver_keys','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','job_callers','job_scheduler','job_tenants','outbox_jobs','files','documents','document_versions','document_acl','knowledge_requests','chunks','knowledge_events','knowledge_selections','knowledge_selection_requests','organization_profiles','organization_requests','private_organization_members','conversations','runs','messages','source_dependencies','feedback','conversation_requests','checkpoints','checkpoint_writes','rag_attempts','rag_citations','rag_runtime','rag_events','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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



