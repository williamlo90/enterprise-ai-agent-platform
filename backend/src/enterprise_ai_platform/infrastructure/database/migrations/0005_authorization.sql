-- Scoped resources are registered by their owning feature/provisioner, never from public role JSON.
CREATE TABLE {schema}.authorization_scopes (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),
    scope_kind text NOT NULL CHECK(scope_kind='knowledge_collection'),
    scope_ref uuid NOT NULL, active boolean NOT NULL DEFAULT true,
    PRIMARY KEY(tenant_id,scope_kind,scope_ref), UNIQUE(scope_kind,scope_ref)
);
ALTER TABLE {schema}.authorization_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.authorization_scopes FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.authorization_scopes TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.authorization_scopes FROM PUBLIC,{access_role};
CREATE FUNCTION {schema}._authorization_scope_valid(tenant uuid,role_name text,kind text,ref text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT coalesce((role_name IN ('employee','tenant_admin') AND kind='tenant' AND ref=tenant::text)
        OR (role_name='knowledge_manager' AND ((kind='tenant' AND ref=tenant::text)
            OR (kind='knowledge_collection' AND EXISTS(SELECT 1 FROM {schema}.authorization_scopes s
                WHERE s.tenant_id=tenant AND s.scope_kind=kind AND s.scope_ref::text=ref AND s.active))))
        OR (role_name IN ('process_owner','approver') AND kind='process' AND ref='reimbursement')
        OR (role_name='operator_scoped' AND kind='operations' AND ref IN ('health','provisioning','job_metadata','config')),false);
$$;
REVOKE ALL ON FUNCTION {schema}._authorization_scope_valid(uuid,text,text,text) FROM PUBLIC,{access_role};

-- AUTH-03 capability; immutable prior migrations and existing role grants are retained.
CREATE TABLE {schema}.role_change_events (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id),
    id uuid NOT NULL, actor_id uuid NOT NULL REFERENCES {schema}.principals(id),
    membership_id uuid NOT NULL, idempotency_key uuid NOT NULL,
    request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{{64}}$'),
    operation text NOT NULL CHECK(operation IN ('grant','revoke')),
    role text NOT NULL, scope_kind text NOT NULL, scope_ref text NOT NULL,
    result_version bigint NOT NULL CHECK(result_version>0),
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,actor_id,idempotency_key),
    FOREIGN KEY(tenant_id,membership_id) REFERENCES {schema}.memberships(tenant_id,id)
);
ALTER TABLE {schema}.role_change_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.role_change_events FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.role_change_events TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.role_change_events FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._authorization_session(token text, touch_activity boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE person uuid; s {schema}.sessions%ROWTYPE; member record; moment timestamptz; enabled boolean;
BEGIN
    SELECT principal_id INTO person FROM {schema}.sessions WHERE token_hash=token;
    IF NOT FOUND THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    SELECT active INTO enabled FROM {schema}.principals WHERE id=person FOR SHARE;
    IF enabled IS NOT TRUE THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    SELECT * INTO s FROM {schema}.sessions WHERE token_hash=token FOR UPDATE;
    IF NOT FOUND OR s.principal_id IS DISTINCT FROM person THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    SELECT m.id,m.access_revision,m.active AND t.active AS active INTO member
        FROM {schema}.memberships m JOIN {schema}.tenants t ON t.id=m.tenant_id
        WHERE m.principal_id=person AND m.tenant_id=s.active_tenant_id FOR SHARE OF m,t;
    moment:=clock_timestamp();
    IF s.revoked_at IS NOT NULL OR moment>=s.expires_at OR moment>=s.last_seen_at+interval '1800 seconds' THEN
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    IF member.id IS NULL OR member.active IS NOT TRUE THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    IF member.access_revision IS DISTINCT FROM s.membership_revision THEN
        UPDATE {schema}.sessions SET revoked_at=moment,updated_at=moment,version=version+1 WHERE id=s.id;
        RETURN jsonb_build_object('status','unauthenticated');
    END IF;
    IF touch_activity THEN
        UPDATE {schema}.sessions SET last_seen_at=moment,updated_at=moment,version=version+1 WHERE id=s.id;
    END IF;
    RETURN jsonb_build_object('status','ok','identity',{schema}._identity_snapshot(person,s.active_tenant_id),
        'grants',coalesce((SELECT jsonb_agg(jsonb_build_object('role',r.role,'scope_kind',r.scope_kind,'scope_ref',r.scope_ref) ORDER BY r.id)
            FROM {schema}.membership_roles r WHERE r.tenant_id=s.active_tenant_id AND r.membership_id=member.id
            AND {schema}._authorization_scope_valid(r.tenant_id,r.role,r.scope_kind,r.scope_ref)),'[]'::jsonb));
END;
$$;
REVOKE ALL ON FUNCTION {schema}._authorization_session(text,boolean) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.authorization_call(token text, operation text, payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE result jsonb; person uuid; tenant uuid; target_person uuid; member record; prior record;
    request_digest text; current_csrf text; changed integer; version_after bigint;
BEGIN
    IF NOT {schema}.runtime_role_is_valid() THEN RAISE EXCEPTION 'Authorization caller rejected.'; END IF;
    IF token IS NULL OR token !~ '^[a-f0-9]{{64}}$' OR payload IS NULL OR jsonb_typeof(payload)<>'object'
       OR octet_length(payload::text)>4096 THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF operation IN ('context','finish') THEN
        IF payload<>'{{}}'::jsonb AND ((SELECT count(*) FROM jsonb_object_keys(payload))<>1
            OR NOT payload ? 'csrf_hash' OR jsonb_typeof(payload->'csrf_hash')<>'string' OR payload->>'csrf_hash' IS NULL
            OR payload->>'csrf_hash' !~ '^[a-f0-9]{{64}}$') THEN RETURN jsonb_build_object('status','invalid'); END IF;
        result:={schema}._authorization_session(token,false);
        IF result->>'status'<>'ok' THEN RETURN result; END IF;
        IF payload ? 'csrf_hash' THEN
            SELECT csrf_hash INTO current_csrf FROM {schema}.sessions WHERE token_hash=token;
            IF current_csrf IS DISTINCT FROM payload->>'csrf_hash' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
        END IF;
        IF operation='finish' THEN RETURN {schema}._authorization_session(token,true); END IF;
        RETURN result;
    END IF;
    IF operation NOT IN ('grant','revoke') OR operation IS NULL THEN RETURN jsonb_build_object('status','invalid'); END IF;
    IF EXISTS(SELECT 1 FROM jsonb_each(payload) e WHERE e.key<>'expected_version' AND jsonb_typeof(e.value)<>'string')
       OR (SELECT count(*) FROM jsonb_object_keys(payload))<>8 OR NOT payload ?& ARRAY[
        'membership_id','expected_version','role','scope_kind','scope_ref','idempotency_key','csrf_hash','operation']
       OR payload->>'operation' IS DISTINCT FROM operation
       OR payload->>'csrf_hash' IS NULL OR payload->>'csrf_hash' !~ '^[a-f0-9]{{64}}$'
       OR payload->>'membership_id' !~ '^[a-f0-9]{{8}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{12}}$'
       OR payload->>'idempotency_key' !~ '^[a-f0-9]{{8}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{12}}$'
       OR jsonb_typeof(payload->'expected_version')<>'number' OR payload->>'expected_version' !~ '^[1-9][0-9]{{0,17}}$' THEN
        RETURN jsonb_build_object('status','invalid');
    END IF;
    SELECT principal_id,active_tenant_id INTO person,tenant FROM {schema}.sessions WHERE token_hash=token;
    IF person IS NULL THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
    -- Serialize role changes, then follow the same principal -> session -> membership order as identity.
    PERFORM pg_advisory_xact_lock(hashtextextended('authorization-role:'||tenant::text,0));
    SELECT principal_id INTO target_person FROM {schema}.memberships
        WHERE tenant_id=tenant AND id=(payload->>'membership_id')::uuid;
    PERFORM id FROM {schema}.principals WHERE id IN (person,target_person) ORDER BY id FOR UPDATE;
    result:={schema}._authorization_session(token,false);
    IF result->>'status'<>'ok' THEN RETURN result; END IF;
    SELECT csrf_hash INTO current_csrf FROM {schema}.sessions WHERE token_hash=token;
    IF current_csrf IS DISTINCT FROM payload->>'csrf_hash' THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    IF NOT EXISTS (SELECT 1 FROM {schema}.membership_roles r JOIN {schema}.memberships m
        ON m.tenant_id=r.tenant_id AND m.id=r.membership_id
        WHERE m.tenant_id=tenant AND m.principal_id=person AND m.active
          AND r.role='tenant_admin' AND r.scope_kind='tenant' AND r.scope_ref=tenant::text) THEN
        RETURN jsonb_build_object('status','forbidden');
    END IF;
    SELECT * INTO member FROM {schema}.memberships
        WHERE tenant_id=tenant AND id=(payload->>'membership_id')::uuid FOR UPDATE;
    IF NOT FOUND OR member.active IS NOT TRUE OR NOT EXISTS(SELECT 1 FROM {schema}.principals WHERE id=member.principal_id AND active) THEN
        RETURN jsonb_build_object('status','not_found');
    END IF;
    IF member.principal_id=person THEN RETURN jsonb_build_object('status','forbidden'); END IF;
    result:={schema}._authorization_session(token,false);
    IF result->>'status'<>'ok' THEN RETURN result; END IF;
    -- No wildcards, tenant aliases, arbitrary process names, or tenant-admin grants of operator authority.
    IF payload->>'role' IS NULL OR payload->>'role' NOT IN ('employee','knowledge_manager','tenant_admin','process_owner','approver')
        OR payload->>'scope_kind' IS NULL OR payload->>'scope_ref' IS NULL
        OR (operation='grant' AND NOT {schema}._authorization_scope_valid(tenant,payload->>'role',payload->>'scope_kind',payload->>'scope_ref')) THEN
        RETURN jsonb_build_object('status','invalid');
    END IF;
    IF operation='grant' AND payload->>'scope_kind'='knowledge_collection' THEN
        PERFORM scope_ref FROM {schema}.authorization_scopes WHERE tenant_id=tenant AND scope_kind='knowledge_collection'
            AND scope_ref::text=payload->>'scope_ref' AND active FOR SHARE;
        IF NOT FOUND THEN RETURN jsonb_build_object('status','invalid'); END IF;
    END IF;
    request_digest:=encode(sha256(convert_to((payload-'csrf_hash')::text,'UTF8')),'hex');
    SELECT * INTO prior FROM {schema}.role_change_events WHERE tenant_id=tenant AND actor_id=person
        AND idempotency_key=(payload->>'idempotency_key')::uuid;
    IF FOUND THEN
        IF prior.request_hash<>request_digest THEN RETURN jsonb_build_object('status','conflict'); END IF;
        result:={schema}._authorization_session(token,true);
        IF result->>'status'<>'ok' THEN RETURN result; END IF;
        RETURN jsonb_build_object('status','ok','membership_version',prior.result_version,'replayed',true);
    END IF;
    IF member.version<>(payload->>'expected_version')::bigint THEN RETURN jsonb_build_object('status','conflict'); END IF;
    -- Recheck the original absolute/idle clocks after any contended target lock, before writes.
    result:={schema}._authorization_session(token,false);
    IF result->>'status'<>'ok' THEN RETURN result; END IF;
    IF operation='grant' THEN
        INSERT INTO {schema}.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by)
            VALUES(tenant,gen_random_uuid(),member.id,payload->>'role',payload->>'scope_kind',payload->>'scope_ref',person)
            ON CONFLICT(tenant_id,membership_id,role,scope_kind,scope_ref) DO NOTHING;
    ELSE
        DELETE FROM {schema}.membership_roles WHERE tenant_id=tenant AND membership_id=member.id
            AND role=payload->>'role' AND scope_kind=payload->>'scope_kind' AND scope_ref=payload->>'scope_ref';
    END IF;
    GET DIAGNOSTICS changed=ROW_COUNT;
    SELECT version INTO version_after FROM {schema}.memberships WHERE tenant_id=tenant AND id=member.id;
    INSERT INTO {schema}.role_change_events(tenant_id,id,actor_id,membership_id,idempotency_key,request_hash,
        operation,role,scope_kind,scope_ref,result_version)
        VALUES(tenant,gen_random_uuid(),person,member.id,(payload->>'idempotency_key')::uuid,request_digest,
            operation,payload->>'role',payload->>'scope_kind',payload->>'scope_ref',version_after);
    result:={schema}._authorization_session(token,true);
    IF result->>'status'<>'ok' THEN RAISE EXCEPTION 'Authorization expired before commit.'; END IF;
    RETURN jsonb_build_object('status','ok','membership_version',version_after,'replayed',false,'changed',changed=1);
END;
$$;
REVOKE ALL ON FUNCTION {schema}.authorization_call(text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.authorization_call(text,text,jsonb) TO {access_role};

-- Preserve the complete runtime role guard, allowing only the new exact signature.
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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

