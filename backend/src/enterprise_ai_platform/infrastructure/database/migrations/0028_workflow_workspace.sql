-- BD-FE-05: scoped browser workflow reads; existing mutation contracts unchanged.
ALTER TABLE {schema}.internal_caller_credentials DROP CONSTRAINT internal_caller_credentials_allowed_actions_check;
ALTER TABLE {schema}.internal_caller_credentials ADD CHECK(jsonb_typeof(allowed_actions)='array' AND jsonb_array_length(allowed_actions)>0 AND allowed_actions <@ '["trip.read","claim.read","operation.read","claim.submit","claim.information","claim.decide","trips.list","policy.publish","workflow.read"]'::jsonb);
CREATE OR REPLACE FUNCTION {schema}.delegation_call(operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE caller {schema}.internal_caller_credentials%ROWTYPE; signing {schema}.delegation_keys%ROWTYPE;
    claims jsonb; person uuid; member record; actor_session {schema}.sessions%ROWTYPE; moment timestamptz; epoch bigint; prior record; replayed boolean:=false;
BEGIN
    IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Internal caller rejected.'; END IF;
    IF payload IS NULL OR jsonb_typeof(payload)<>'object' OR octet_length(payload::text)>16384
        OR payload->>'credential_hash' IS NULL OR payload->>'credential_hash' !~ '^[a-f0-9]{{64}}$' THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    SELECT * INTO caller FROM {schema}.internal_caller_credentials WHERE credential_hash=payload->>'credential_hash' FOR SHARE;
    IF NOT FOUND OR caller.active IS NOT TRUE OR clock_timestamp()>=caller.expires_at THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    IF operation='caller' THEN
        IF (SELECT count(*) FROM jsonb_object_keys(payload))<>1 THEN RETURN jsonb_build_object('status','invalid'); END IF;
        RETURN jsonb_build_object('status','ok','caller_ref',caller.caller_ref,'audience',caller.audience,'delegation_caller',caller.delegation_caller);
    ELSIF operation='key' THEN
        IF (SELECT count(*) FROM jsonb_object_keys(payload))<>3 OR NOT payload ?& ARRAY['credential_hash','issuer','kid'] THEN
            RETURN jsonb_build_object('status','invalid');
        END IF;
        SELECT * INTO signing FROM {schema}.delegation_keys WHERE issuer=payload->>'issuer' AND kid=payload->>'kid' FOR SHARE;
        IF NOT FOUND OR signing.active IS NOT TRUE OR clock_timestamp()<signing.not_before OR clock_timestamp()>=signing.not_after THEN
            RETURN jsonb_build_object('status','unauthenticated');
        END IF;
        RETURN jsonb_build_object('status','ok','jwk',signing.public_jwk,'key_hash',encode(sha256(convert_to(signing.public_jwk::text,'UTF8')),'hex'));
    ELSIF operation IS DISTINCT FROM 'verify' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(payload))<>5 OR NOT payload ?& ARRAY['credential_hash','claims','key_hash','key_kid','binding_hash']
        OR jsonb_typeof(payload->'claims')<>'object' OR payload->>'binding_hash' IS NULL
        OR payload->>'binding_hash' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid'); END IF;
    claims:=payload->'claims';
    IF NOT claims ?& ARRAY['iss','aud','caller','sub','tenant_id','membership_id','access_revision','action','object_kind','object_id',
        'object_version','request_hash','operation_id','jti','iat','nbf','exp','human_receipt_id','session_id']
        OR (SELECT count(*) FROM jsonb_object_keys(claims))<>(CASE WHEN claims->>'action' IN ('trips.list','policy.publish','workflow.read') THEN 20 ELSE 19 END)
        THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF claims->>'action' IN ('trips.list','policy.publish','workflow.read') THEN
        IF claims->'canonical_version' IS DISTINCT FROM '2'::jsonb OR NOT claims ? 'canonical_version'
          OR claims->'human_receipt_id' IS DISTINCT FROM 'null'::jsonb
          OR (claims->>'action'='trips.list' AND (claims->>'object_kind' IS DISTINCT FROM 'trip_collection'
             OR claims->'object_id' IS DISTINCT FROM 'null'::jsonb OR claims->'object_version' IS DISTINCT FROM '1'::jsonb))
          OR (claims->>'action'='workflow.read' AND (claims->>'object_kind' NOT IN ('employee_workspace','approval_workspace') OR claims->'object_version' IS DISTINCT FROM '1'::jsonb))
          OR (claims->>'action'='policy.publish' AND (claims->>'object_kind' IS DISTINCT FROM 'policy_package' OR claims->>'object_id' IS NULL))
          THEN RETURN jsonb_build_object('status','invalid'); END IF;
    ELSIF claims ? 'canonical_version' OR claims->>'action' NOT IN ('trip.read','claim.read','operation.read','claim.submit','claim.information','claim.decide')
      THEN RETURN jsonb_build_object('status','invalid'); END IF;
    SELECT * INTO signing FROM {schema}.delegation_keys WHERE issuer=claims->>'iss' AND kid=payload->>'key_kid'
        AND encode(sha256(convert_to(public_jwk::text,'UTF8')),'hex')=payload->>'key_hash' FOR SHARE;
    IF NOT FOUND OR signing.active IS NOT TRUE OR claims->>'aud' IS DISTINCT FROM caller.audience
        OR claims->>'caller' IS DISTINCT FROM caller.delegation_caller
        OR NOT caller.allowed_actions ? (claims->>'action') THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    person:=(claims->>'sub')::uuid;
    PERFORM id FROM {schema}.principals WHERE id=person AND active FOR SHARE;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    SELECT * INTO actor_session FROM {schema}.sessions WHERE id=(claims->>'session_id')::uuid FOR SHARE;
    IF NOT FOUND OR actor_session.principal_id IS DISTINCT FROM person
        OR actor_session.active_tenant_id IS DISTINCT FROM (claims->>'tenant_id')::uuid
        OR actor_session.membership_revision IS DISTINCT FROM (claims->>'access_revision')::bigint
        OR actor_session.revoked_at IS NOT NULL THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    SELECT m.id,m.access_revision INTO member FROM {schema}.memberships m JOIN {schema}.tenants t ON t.id=m.tenant_id
        WHERE m.tenant_id=(claims->>'tenant_id')::uuid AND m.id=(claims->>'membership_id')::uuid
          AND m.principal_id=person AND m.active AND t.active FOR SHARE OF m,t;
    IF NOT FOUND OR member.access_revision<>(claims->>'access_revision')::bigint THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    IF NOT EXISTS(SELECT 1 FROM {schema}.membership_roles r WHERE r.tenant_id=(claims->>'tenant_id')::uuid
        AND r.membership_id=member.id AND {schema}._authorization_scope_valid(r.tenant_id,r.role,r.scope_kind,r.scope_ref)
        AND ((r.role='employee' AND r.scope_kind='tenant' AND r.scope_ref=(claims->>'tenant_id')
             AND claims->>'action' IN ('trip.read','claim.read','operation.read','claim.submit','claim.information','trips.list'))
          OR (r.role='approver' AND claims->>'action' IN ('claim.read','operation.read','claim.decide'))
          OR (claims->>'action'='workflow.read' AND ((r.role='employee' AND r.scope_kind='tenant' AND r.scope_ref=claims->>'tenant_id' AND claims->>'object_kind'='employee_workspace') OR (r.role='approver' AND r.scope_kind='process' AND r.scope_ref='reimbursement' AND claims->>'object_kind'='approval_workspace')))
          OR (r.role='process_owner' AND r.scope_kind='process' AND r.scope_ref='reimbursement' AND claims->>'action'='policy.publish'))) THEN
        RETURN jsonb_build_object('status','forbidden');
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('delegation-replay:v1',0));
    moment:=clock_timestamp();epoch:=floor(extract(epoch FROM moment))::bigint;
    IF moment>=actor_session.expires_at OR moment>=actor_session.last_seen_at+interval '1800 seconds'
        OR caller.active IS NOT TRUE OR moment>=caller.expires_at OR signing.active IS NOT TRUE OR moment<signing.not_before OR moment>=signing.not_after
        OR (claims->>'iat')::bigint>epoch+5 OR (claims->>'iat')::bigint<=0
        OR claims->>'nbf' IS DISTINCT FROM claims->>'iat' OR (claims->>'exp')::bigint<=epoch
        OR (claims->>'exp')::bigint<=(claims->>'iat')::bigint OR (claims->>'exp')::bigint>(claims->>'iat')::bigint+60
        OR to_timestamp((claims->>'exp')::double precision)>signing.not_after THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    DELETE FROM {schema}.delegation_replays WHERE expires_at<=moment;
    SELECT * INTO prior FROM {schema}.delegation_replays WHERE issuer=claims->>'iss' AND jti=(claims->>'jti')::uuid;
    IF FOUND THEN
        IF prior.binding_hash IS DISTINCT FROM payload->>'binding_hash' THEN RETURN jsonb_build_object('status','conflict'); END IF;
        replayed:=true;
    ELSE
        IF (SELECT count(*) FROM {schema}.delegation_replays)>=100000 THEN RETURN jsonb_build_object('status','unavailable'); END IF;
        INSERT INTO {schema}.delegation_replays(issuer,jti,binding_hash,operation_id,expires_at)
            VALUES(claims->>'iss',(claims->>'jti')::uuid,payload->>'binding_hash',(claims->>'operation_id')::uuid,to_timestamp((claims->>'exp')::double precision));
    END IF;
    moment:=clock_timestamp();epoch:=floor(extract(epoch FROM moment))::bigint;
    IF moment>=least(actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds',
        caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision)) THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    IF claims->>'action' IN ('claim.submit','claim.information','claim.decide') THEN
        DELETE FROM {schema}.delegation_verification_contexts WHERE expires_at<=moment;
        IF NOT EXISTS(SELECT 1 FROM {schema}.delegation_verification_contexts
          WHERE issuer=claims->>'iss' AND jti=(claims->>'jti')::uuid AND credential_hash=payload->>'credential_hash'
            AND key_kid=payload->>'key_kid' AND key_hash=payload->>'key_hash') THEN
            IF (SELECT count(*) FROM {schema}.delegation_verification_contexts)>=100000 THEN RETURN jsonb_build_object('status','unavailable'); END IF;
            INSERT INTO {schema}.delegation_verification_contexts
              VALUES(claims->>'iss',(claims->>'jti')::uuid,payload->>'credential_hash',payload->>'key_kid',payload->>'key_hash',payload->>'binding_hash',
                jsonb_build_object('claims',claims,'caller_ref',caller.caller_ref,'audience',caller.audience,'delegation_caller',caller.delegation_caller),
                least(moment+interval '60 seconds',actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds',caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision)));
        ELSE
            IF EXISTS(SELECT 1 FROM {schema}.delegation_verification_contexts
              WHERE issuer=claims->>'iss' AND jti=(claims->>'jti')::uuid AND credential_hash=payload->>'credential_hash'
                AND key_kid=payload->>'key_kid' AND key_hash=payload->>'key_hash'
                AND (binding_hash IS DISTINCT FROM payload->>'binding_hash' OR context IS DISTINCT FROM
                  jsonb_build_object('claims',claims,'caller_ref',caller.caller_ref,'audience',caller.audience,'delegation_caller',caller.delegation_caller)))
              THEN RETURN jsonb_build_object('status','conflict'); END IF;
        END IF;
    END IF;
    RETURN jsonb_build_object('status','ok','access_revision',member.access_revision,'verified_at',epoch,'replayed',replayed,
        'authority_expires_at',extract(epoch FROM least(actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds',
            caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision))));
END;
$$;

CREATE FUNCTION {schema}.workspace_call(token text,operation text,payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE auth jsonb;i jsonb;ctx jsonb;result jsonb;rows jsonb;view text:=payload->>'view';t uuid;p uuid;s {schema}.sessions%ROWTYPE;deadline numeric;lim integer;cursor_id uuid;
BEGIN
 IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Workspace capability rejected';END IF;
 IF operation IS NULL OR operation NOT IN ('context','drafts','actions','rules') OR jsonb_typeof(payload) IS DISTINCT FROM 'object' OR view IS NULL OR view NOT IN ('mine','approvals') THEN RETURN jsonb_build_object('status','invalid');END IF;
 auth:={schema}.authorization_call(token,'context','{{}}'::jsonb);
 IF auth->>'status' IS DISTINCT FROM 'ok' THEN RETURN auth;END IF;
 i:=auth->'identity';t:=(i->>'active_tenant_id')::uuid;p:=(i->>'principal_id')::uuid;
 IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(auth->'grants') g WHERE
   (view='mine' AND g->>'role'='employee' AND g->>'scope_kind'='tenant' AND g->>'scope_ref'=t::text) OR
   ((view='approvals' OR operation='actions') AND g->>'role'='approver' AND g->>'scope_kind'='process' AND g->>'scope_ref'='reimbursement')) THEN RETURN jsonb_build_object('status','forbidden');END IF;
 SELECT * INTO s FROM {schema}.sessions WHERE token_hash=token FOR SHARE;
 deadline:=extract(epoch FROM least(s.expires_at,s.last_seen_at+interval '1800 seconds'));
 ctx:=jsonb_build_object('tenant_id',t,'actor_id',p,'membership_id',i->>'active_membership_id','session_id',s.id,'access_revision',i->'access_revision','authority_expires_at',deadline);
 IF operation='context' THEN result:=ctx;
 ELSIF operation='rules' THEN
  IF view<>'mine' OR payload->>'material_hash' IS NULL OR payload->>'material_hash' !~ '^[a-f0-9]{{64}}$' THEN RETURN jsonb_build_object('status','invalid');END IF;
  SELECT jsonb_build_object('draft_id',d.id,'draft_version',d.current_version,'material_hash',pr.material_hash,'policy',pr.material->'data'->'policy','timezone',pkg.timezone,'rules',pkg.rules_json) INTO result
   FROM {schema}.drafts d JOIN {schema}.draft_previews pr ON pr.tenant_id=d.tenant_id AND pr.draft_id=d.id AND pr.version=d.current_version AND pr.material_hash=payload->>'material_hash'
   JOIN {schema}.policy_packages pkg ON pkg.tenant_id=d.tenant_id AND pkg.id=(pr.material->'data'->'policy'->>'id')::uuid AND pkg.package_version=(pr.material->'data'->'policy'->>'version')::bigint AND pkg.digest=pr.material->'data'->'policy'->>'digest'
   WHERE d.tenant_id=t AND d.owner_principal_id=p AND d.id=(payload->>'id')::uuid FOR SHARE OF d,pr,pkg;
  IF NOT FOUND THEN RETURN jsonb_build_object('status','conflict');END IF;
 ELSE
  IF view<>'mine' OR NOT payload ?& ARRAY['after','limit'] OR jsonb_typeof(payload->'limit') IS DISTINCT FROM 'number' OR payload->>'limit' !~ '^[1-9][0-9]?$' THEN RETURN jsonb_build_object('status','invalid');END IF;
  lim:=(payload->>'limit')::integer;cursor_id:=(payload->>'after')::uuid;
  IF lim NOT BETWEEN 1 AND 20 THEN RETURN jsonb_build_object('status','invalid');END IF;
  IF operation='drafts' THEN
   IF cursor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.drafts WHERE tenant_id=t AND id=cursor_id AND owner_principal_id=p) THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY id),'[]'::jsonb) INTO rows FROM (
    SELECT d.id,jsonb_build_object('id',d.id,'kind',d.kind,'trip_id',d.trip_ref,'claim_id',d.claim_ref,'conversation_id',d.conversation_id,'version',d.current_version,'state',d.state) value
    FROM {schema}.drafts d WHERE tenant_id=t AND owner_principal_id=p AND (cursor_id IS NULL OR id>cursor_id) ORDER BY id LIMIT lim+1) q;
  ELSE
   IF cursor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM {schema}.actions WHERE tenant_id=t AND id=cursor_id AND actor_id=p) THEN RETURN jsonb_build_object('status','conflict');END IF;
   SELECT coalesce(jsonb_agg(value ORDER BY id),'[]'::jsonb) INTO rows FROM (
    SELECT a.id,{schema}._action_json(a) value FROM {schema}.actions a WHERE tenant_id=t AND actor_id=p AND (cursor_id IS NULL OR id>cursor_id) ORDER BY id LIMIT lim+1) q;
  END IF;
  result:=jsonb_build_object('items',CASE WHEN jsonb_array_length(rows)>lim THEN rows-lim ELSE rows END,'next_after',CASE WHEN jsonb_array_length(rows)>lim THEN rows->(lim-1)->>(CASE WHEN operation='actions' THEN 'operation_id' ELSE 'id' END) END);
 END IF;
 auth:={schema}.authorization_call(token,'finish','{{}}'::jsonb);
 IF auth->>'status' IS DISTINCT FROM 'ok' OR extract(epoch FROM clock_timestamp())>=deadline THEN RAISE EXCEPTION 'Workspace authority expired' USING ERRCODE='40001';END IF;
 RETURN result||jsonb_build_object('status','ok','authority_expires_at',deadline);
EXCEPTION WHEN data_exception THEN RETURN jsonb_build_object('status','invalid');
END $$;
REVOKE ALL ON FUNCTION {schema}.workspace_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.workspace_call(text,text,jsonb) TO {access_role};
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



