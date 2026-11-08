-- AUTH-04: private service credentials, verification keys, and durable replay binding.
CREATE TABLE {schema}.internal_caller_credentials (
    id uuid PRIMARY KEY, caller_ref text NOT NULL CHECK(caller_ref ~ '^[a-z][a-z0-9_.-]{{0,63}}$'),
    credential_hash text NOT NULL UNIQUE CHECK(credential_hash ~ '^[a-f0-9]{{64}}$'),
    audience text NOT NULL CHECK(audience ~ '^[a-z][a-z0-9_.-]{{0,63}}$'),
    delegation_caller text NOT NULL CHECK(delegation_caller ~ '^[a-z][a-z0-9_.-]{{0,63}}$'),
    allowed_actions jsonb NOT NULL CHECK(jsonb_typeof(allowed_actions)='array' AND jsonb_array_length(allowed_actions) BETWEEN 1 AND 6
        AND allowed_actions <@ '["trip.read","claim.read","operation.read","claim.submit","claim.information","claim.decide"]'::jsonb),
    active boolean NOT NULL DEFAULT true, expires_at timestamptz NOT NULL
);
CREATE TABLE {schema}.delegation_keys (
    issuer text NOT NULL CHECK(octet_length(issuer) BETWEEN 1 AND 128),kid text NOT NULL CHECK(kid ~ '^[A-Za-z0-9_.-]{{1,64}}$'),
    public_jwk jsonb NOT NULL CHECK(jsonb_typeof(public_jwk)='object' AND octet_length(public_jwk::text)<=4096),
    active boolean NOT NULL DEFAULT true, not_before timestamptz NOT NULL, not_after timestamptz NOT NULL CHECK(not_after>not_before),
    PRIMARY KEY(issuer,kid)
);
CREATE TABLE {schema}.delegation_replays (
    issuer text NOT NULL,jti uuid NOT NULL,binding_hash text NOT NULL CHECK(binding_hash ~ '^[a-f0-9]{{64}}$'),
    operation_id uuid NOT NULL,expires_at timestamptz NOT NULL,verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(issuer,jti)
);
CREATE INDEX delegation_replays_expiry ON {schema}.delegation_replays(expires_at);
ALTER TABLE {schema}.internal_caller_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.internal_caller_credentials FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.delegation_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.delegation_keys FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.delegation_replays ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.delegation_replays FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.internal_caller_credentials TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.delegation_keys TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.delegation_replays TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.internal_caller_credentials,{schema}.delegation_keys,{schema}.delegation_replays FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.delegation_call(operation text,payload jsonb) RETURNS jsonb
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
        OR (SELECT count(*) FROM jsonb_object_keys(claims))<>19 THEN RETURN jsonb_build_object('status','invalid'); END IF;
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
        AND ((r.role='employee' AND claims->>'action'<>'claim.decide')
          OR (r.role='approver' AND claims->>'action' IN ('claim.read','operation.read','claim.decide')))) THEN
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
    RETURN jsonb_build_object('status','ok','access_revision',member.access_revision,'verified_at',epoch,'replayed',replayed,
        'authority_expires_at',extract(epoch FROM least(actor_session.expires_at,actor_session.last_seen_at+interval '1800 seconds',
            caller.expires_at,signing.not_after,to_timestamp((claims->>'exp')::double precision))));
END;
$$;
REVOKE ALL ON FUNCTION {schema}.delegation_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.delegation_call(text,jsonb) TO {access_role};

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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','role_change_events','authorization_scopes','internal_caller_credentials','delegation_keys','delegation_replays','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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

