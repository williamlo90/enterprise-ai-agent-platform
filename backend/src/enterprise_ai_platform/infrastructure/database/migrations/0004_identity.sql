-- Existing revisions remain immutable. Unknown-age legacy sessions fail closed.
ALTER TABLE {schema}.memberships ADD CONSTRAINT memberships_actor_id_unique UNIQUE(principal_id,id);
ALTER TABLE {schema}.sessions ADD COLUMN authenticated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE {schema}.sessions ADD COLUMN membership_revision bigint;
ALTER TABLE {schema}.sessions ADD COLUMN last_seen_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP;
UPDATE {schema}.sessions SET revoked_at=greatest(created_at,clock_timestamp()),
    authenticated_at=created_at,last_seen_at=created_at,
    token_hash=CASE WHEN token_hash ~ '^[a-f0-9]{{64}}$' THEN token_hash ELSE encode(sha256(convert_to('revoked-session:'||id::text,'UTF8')),'hex') END,
    csrf_hash=CASE WHEN csrf_hash ~ '^[a-f0-9]{{64}}$' THEN csrf_hash ELSE encode(sha256(convert_to('revoked-csrf:'||id::text,'UTF8')),'hex') END;
ALTER TABLE {schema}.sessions ADD CONSTRAINT session_hash_format
    CHECK (token_hash ~ '^[a-f0-9]{{64}}$' AND csrf_hash ~ '^[a-f0-9]{{64}}$');
ALTER TABLE {schema}.sessions ADD CONSTRAINT session_clock_order
    CHECK (last_seen_at >= authenticated_at AND (revoked_at IS NOT NULL OR
        (expires_at=authenticated_at+interval '8 hours' AND created_at>=authenticated_at)));
CREATE INDEX sessions_idle_expiry ON {schema}.sessions(last_seen_at) WHERE revoked_at IS NULL;

CREATE TABLE {schema}.login_transactions (
    id uuid PRIMARY KEY, binding_hash text NOT NULL UNIQUE CHECK(binding_hash ~ '^[a-f0-9]{{64}}$'),
    state_hash text NOT NULL CHECK(state_hash ~ '^[a-f0-9]{{64}}$'),
    nonce_hash text NOT NULL CHECK(nonce_hash ~ '^[a-f0-9]{{64}}$'),
    verifier text CHECK(verifier IS NULL OR octet_length(verifier) BETWEEN 1 AND 512),
    issuer text NOT NULL CHECK(octet_length(issuer) BETWEEN 1 AND 2048),
    client_id text NOT NULL CHECK(octet_length(client_id) BETWEEN 1 AND 256),
    redirect_uri text NOT NULL CHECK(octet_length(redirect_uri) BETWEEN 1 AND 4096),
    return_to text NOT NULL CHECK(return_to IN ('home','tenant_picker')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL, consumed_at timestamptz,
    CHECK(expires_at=created_at+interval '300 seconds'),
    CHECK(consumed_at IS NULL OR verifier IS NULL)
);
CREATE INDEX login_transactions_expiry ON {schema}.login_transactions(expires_at);
CREATE TABLE {schema}.identity_limits (
    key text PRIMARY KEY CHECK(key ~ '^(login|callback):(ip|binding):[a-f0-9]{{64}}$'),
    started_at timestamptz NOT NULL, requests integer NOT NULL CHECK(requests BETWEEN 1 AND 10)
);
CREATE INDEX identity_limits_expiry ON {schema}.identity_limits(started_at);
CREATE TABLE {schema}.identity_jwks (
    issuer text PRIMARY KEY CHECK(octet_length(issuer) BETWEEN 1 AND 2048),
    keys jsonb NOT NULL DEFAULT '[]', fetched_at timestamptz,
    attempted_at timestamptz, lease_hash text, lease_until timestamptz,
    CHECK(jsonb_typeof(keys)='array' AND jsonb_array_length(keys)<=32 AND octet_length(keys::text)<=262144)
);
CREATE TABLE {schema}.identity_negative_keys (
    issuer text NOT NULL REFERENCES {schema}.identity_jwks(issuer) ON DELETE CASCADE,
    kid text NOT NULL CHECK(octet_length(kid) BETWEEN 1 AND 128),
    expires_at timestamptz NOT NULL, PRIMARY KEY(issuer,kid)
);

ALTER TABLE {schema}.login_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.login_transactions FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.identity_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.identity_limits FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.identity_jwks ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.identity_jwks FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.identity_negative_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.identity_negative_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.login_transactions TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.identity_limits TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.identity_jwks TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.identity_negative_keys TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON {schema}.login_transactions,{schema}.identity_limits,{schema}.identity_jwks,
    {schema}.identity_negative_keys FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}._identity_snapshot(person uuid, tenant uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
    SELECT jsonb_build_object('principal_id',p.id,'active_tenant_id',chosen.tenant_id,
        'active_membership_id',chosen.membership_id,'access_revision',chosen.access_revision,
        'memberships',coalesce((SELECT jsonb_agg(jsonb_build_object('membership_id',m.id,
            'tenant_id',m.tenant_id,'access_revision',m.access_revision,'tenant_name',t.name,
            'roles',coalesce((SELECT jsonb_agg(jsonb_build_object('role',r.role,'scope_kind',r.scope_kind,'scope_ref',r.scope_ref) ORDER BY r.id)
                FROM {schema}.membership_roles r WHERE r.tenant_id=m.tenant_id AND r.membership_id=m.id),'[]'::jsonb)) ORDER BY m.id)
          FROM {schema}.memberships m JOIN {schema}.tenants t ON t.id=m.tenant_id
          WHERE m.principal_id=p.id AND m.active AND t.active),'[]'::jsonb))
    FROM {schema}.principals p LEFT JOIN LATERAL (
        SELECT m.id membership_id,m.tenant_id,m.access_revision FROM {schema}.memberships m JOIN {schema}.tenants t ON t.id=m.tenant_id
        WHERE m.principal_id=p.id AND m.tenant_id=tenant AND m.active AND t.active) chosen ON true
    WHERE p.id=person AND p.active;
$$;
REVOKE ALL ON FUNCTION {schema}._identity_snapshot(uuid,uuid) FROM PUBLIC,{access_role};

CREATE FUNCTION {schema}.identity_call(operation text, payload jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
    moment timestamptz := clock_timestamp();
    tx {schema}.login_transactions%ROWTYPE;
    session_row {schema}.sessions%ROWTYPE;
    cache {schema}.identity_jwks%ROWTYPE;
    bucket {schema}.identity_limits%ROWTYPE;
    item text; wait_seconds integer; old_verifier text; person uuid; old_person uuid; member record;
    principal_active boolean; snapshot jsonb; known boolean; lease boolean; negative boolean;
BEGIN
    IF NOT {schema}.runtime_role_is_valid() OR jsonb_typeof(payload) IS DISTINCT FROM 'object'
       OR octet_length(payload::text)>300000 THEN
        RAISE EXCEPTION 'Identity capability rejected.' USING ERRCODE='42501';
    END IF;
    IF operation='limit' THEN
        IF jsonb_typeof(payload->'keys') IS DISTINCT FROM 'array'
           OR jsonb_array_length(payload->'keys') NOT BETWEEN 1 AND 2 THEN
            RAISE EXCEPTION 'Identity input rejected.';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('{schema}:identity:limits',0));
        DELETE FROM {schema}.identity_limits WHERE started_at<=moment-interval '60 seconds';
        FOR item IN SELECT jsonb_array_elements_text(payload->'keys') LOOP
            IF item !~ '^(login|callback):(ip|binding):[a-f0-9]{{64}}$' THEN RAISE EXCEPTION 'Identity input rejected.'; END IF;
            SELECT * INTO bucket FROM {schema}.identity_limits WHERE key=item;
            IF FOUND AND bucket.requests>=10 THEN
                wait_seconds:=greatest(1,ceil(extract(epoch FROM bucket.started_at+interval '60 seconds'-moment))::integer);
                RETURN jsonb_build_object('status','rate_limited','retry_after',wait_seconds);
            END IF;
        END LOOP;
        IF (SELECT count(*) FROM {schema}.identity_limits)+
           (SELECT count(*) FROM jsonb_array_elements_text(payload->'keys') k WHERE NOT EXISTS
              (SELECT 1 FROM {schema}.identity_limits b WHERE b.key=k))>100000 THEN
            RETURN jsonb_build_object('status','unavailable');
        END IF;
        FOR item IN SELECT jsonb_array_elements_text(payload->'keys') LOOP
            INSERT INTO {schema}.identity_limits(key,started_at,requests) VALUES(item,moment,1)
              ON CONFLICT(key) DO UPDATE SET requests={schema}.identity_limits.requests+1;
        END LOOP;
        RETURN jsonb_build_object('status','ok');
    ELSIF operation='login_start' THEN
        DELETE FROM {schema}.login_transactions WHERE expires_at<=moment;
        IF payload->>'old_binding' ~ '^[a-f0-9]{{64}}$' THEN
            DELETE FROM {schema}.login_transactions WHERE binding_hash=payload->>'old_binding';
        END IF;
        INSERT INTO {schema}.login_transactions(id,binding_hash,state_hash,nonce_hash,verifier,issuer,client_id,
            redirect_uri,return_to,created_at,expires_at)
        VALUES((payload->>'id')::uuid,payload->>'binding_hash',payload->>'state_hash',payload->>'nonce_hash',
            payload->>'verifier',payload->>'issuer',payload->>'client_id',payload->>'redirect_uri',payload->>'return_to',
            moment,moment+interval '300 seconds');
        RETURN jsonb_build_object('status','ok');
    ELSIF operation='login_claim' THEN
        SELECT * INTO tx FROM {schema}.login_transactions WHERE binding_hash=payload->>'binding_hash'
            AND state_hash=payload->>'state_hash' FOR UPDATE;
        moment:=clock_timestamp();
        IF NOT FOUND THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
        IF tx.expires_at<=moment THEN
            DELETE FROM {schema}.login_transactions WHERE id=tx.id;
            RETURN jsonb_build_object('status','unauthenticated');
        END IF;
        IF tx.consumed_at IS NOT NULL OR tx.verifier IS NULL THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
        old_verifier:=tx.verifier;
        UPDATE {schema}.login_transactions SET verifier=NULL,consumed_at=moment WHERE id=tx.id;
        RETURN jsonb_build_object('status','ok','id',tx.id,'verifier',old_verifier,'nonce_hash',tx.nonce_hash,
            'issuer',tx.issuer,'client_id',tx.client_id,'redirect_uri',tx.redirect_uri,'return_to',tx.return_to);
    ELSIF operation='login_complete' THEN
      BEGIN
        SELECT * INTO tx FROM {schema}.login_transactions WHERE id=(payload->>'transaction_id')::uuid FOR UPDATE;
        moment:=clock_timestamp();
        IF NOT FOUND OR tx.consumed_at IS NULL OR tx.expires_at<=moment OR tx.issuer<>payload->>'issuer' THEN
            RETURN jsonb_build_object('status','unauthenticated');
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('{schema}:login:'||(payload->>'issuer')||':'||(payload->>'subject'),0));
        SELECT id INTO person FROM {schema}.principals WHERE issuer=payload->>'issuer' AND subject=payload->>'subject';
        SELECT principal_id INTO old_person FROM {schema}.sessions WHERE token_hash=payload->>'old_session_hash';
        PERFORM id FROM {schema}.principals WHERE id IN (person,old_person) ORDER BY id FOR UPDATE;
        PERFORM id FROM {schema}.sessions WHERE token_hash=payload->>'old_session_hash' FOR UPDATE;
        moment:=clock_timestamp();
        IF tx.expires_at<=moment THEN RAISE EXCEPTION 'Login expired.' USING ERRCODE='EA001'; END IF;
        INSERT INTO {schema}.principals(id,issuer,subject) VALUES((payload->>'principal_id')::uuid,
            payload->>'issuer',payload->>'subject') ON CONFLICT(issuer,subject) DO NOTHING;
        SELECT id,active INTO person,principal_active FROM {schema}.principals
            WHERE issuer=payload->>'issuer' AND subject=payload->>'subject' FOR UPDATE;
        IF principal_active IS NOT TRUE THEN RAISE EXCEPTION 'Login rejected.' USING ERRCODE='EA001'; END IF;
        moment:=clock_timestamp();
        IF tx.expires_at<=moment THEN RAISE EXCEPTION 'Login expired.' USING ERRCODE='EA001'; END IF;
        UPDATE {schema}.sessions SET revoked_at=moment,updated_at=moment,version=version+1
            WHERE token_hash=payload->>'old_session_hash' AND revoked_at IS NULL;
        INSERT INTO {schema}.sessions(id,principal_id,token_hash,csrf_hash,created_at,authenticated_at,last_seen_at,expires_at)
            VALUES((payload->>'session_id')::uuid,person,payload->>'token_hash',payload->>'csrf_hash',moment,
                moment,moment,moment+interval '8 hours');
        DELETE FROM {schema}.login_transactions WHERE id=tx.id;
        RETURN jsonb_build_object('status','ok','identity',{schema}._identity_snapshot(person,NULL),'max_age',28800);
      EXCEPTION WHEN SQLSTATE 'EA001' THEN
        RETURN jsonb_build_object('status','unauthenticated');
      END;
    ELSIF operation IN ('me','resolve','logout','switch') THEN
        -- Lock principal before session, matching login completion and avoiding inversion.
        SELECT principal_id INTO person FROM {schema}.sessions WHERE token_hash=payload->>'token_hash';
        IF NOT FOUND THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
        SELECT active INTO principal_active FROM {schema}.principals WHERE id=person FOR SHARE;
        IF principal_active IS NOT TRUE THEN RETURN jsonb_build_object('status','unauthenticated'); END IF;
        SELECT * INTO session_row FROM {schema}.sessions WHERE token_hash=payload->>'token_hash' FOR UPDATE;
        moment:=clock_timestamp();
        IF NOT FOUND OR session_row.principal_id IS DISTINCT FROM person OR session_row.revoked_at IS NOT NULL
           OR moment>=session_row.expires_at OR moment>=session_row.last_seen_at+interval '1800 seconds' THEN
            RETURN jsonb_build_object('status','unauthenticated');
        END IF;
        IF session_row.active_tenant_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM {schema}.memberships m JOIN {schema}.tenants t ON t.id=m.tenant_id
            WHERE m.principal_id=person AND m.tenant_id=session_row.active_tenant_id AND m.active AND t.active
              AND m.access_revision IS DISTINCT FROM session_row.membership_revision) THEN
            UPDATE {schema}.sessions SET revoked_at=moment,updated_at=moment,version=version+1 WHERE id=session_row.id;
            RETURN jsonb_build_object('status','unauthenticated');
        END IF;
        IF operation IN ('logout','switch') AND session_row.csrf_hash IS DISTINCT FROM payload->>'csrf_hash' THEN
            RETURN jsonb_build_object('status','forbidden');
        END IF;
        IF operation='me' THEN
            IF payload->>'new_csrf_hash' !~ '^[a-f0-9]{{64}}$' THEN RAISE EXCEPTION 'Identity input rejected.'; END IF;
            UPDATE {schema}.sessions SET csrf_hash=payload->>'new_csrf_hash',version=version+1,updated_at=moment
                WHERE id=session_row.id;
        ELSIF operation='logout' THEN
            UPDATE {schema}.sessions SET revoked_at=moment,version=version+1,updated_at=moment WHERE id=session_row.id;
            RETURN jsonb_build_object('status','ok');
        ELSIF operation='switch' THEN
            SELECT m.id,m.tenant_id,m.access_revision INTO member FROM {schema}.memberships m
                JOIN {schema}.tenants t ON t.id=m.tenant_id WHERE m.id=(payload->>'membership_id')::uuid
                AND m.principal_id=session_row.principal_id AND m.active AND t.active FOR SHARE OF m,t;
            IF NOT FOUND THEN RETURN jsonb_build_object('status','forbidden'); END IF;
            moment:=clock_timestamp();
            IF moment>=session_row.expires_at OR moment>=session_row.last_seen_at+interval '1800 seconds' THEN
                RETURN jsonb_build_object('status','unauthenticated');
            END IF;
            UPDATE {schema}.sessions SET revoked_at=moment,version=version+1,updated_at=moment WHERE id=session_row.id;
            INSERT INTO {schema}.sessions(id,principal_id,token_hash,csrf_hash,active_tenant_id,membership_revision,
                authenticated_at,last_seen_at,created_at,expires_at)
                VALUES((payload->>'session_id')::uuid,session_row.principal_id,payload->>'new_token_hash',
                    payload->>'new_csrf_hash',member.tenant_id,member.access_revision,session_row.authenticated_at,moment,moment,session_row.expires_at);
            session_row.active_tenant_id:=member.tenant_id;
        END IF;
        snapshot:={schema}._identity_snapshot(session_row.principal_id,session_row.active_tenant_id);
        IF operation='resolve' AND snapshot->>'active_tenant_id' IS NULL THEN
            RETURN jsonb_build_object('status','forbidden');
        END IF;
        IF operation='resolve' THEN
            UPDATE {schema}.sessions SET last_seen_at=moment,version=version+1,updated_at=moment WHERE id=session_row.id;
        END IF;
        RETURN jsonb_build_object('status','ok','identity',snapshot,
            'max_age',greatest(0,floor(extract(epoch FROM session_row.expires_at-moment))::integer));
    ELSIF operation='jwks_reserve' THEN
        IF payload->>'lease_hash' IS NULL OR payload->>'lease_hash' !~ '^[a-f0-9]{{64}}$'
           OR octet_length(payload->>'issuer') NOT BETWEEN 1 AND 2048 OR octet_length(payload->>'kid')>128
           OR payload->>'lease_hash' !~ '^[a-f0-9]{{64}}$' THEN RAISE EXCEPTION 'Identity input rejected.'; END IF;
        INSERT INTO {schema}.identity_jwks(issuer) VALUES(payload->>'issuer') ON CONFLICT DO NOTHING;
        SELECT * INTO cache FROM {schema}.identity_jwks WHERE issuer=payload->>'issuer' FOR UPDATE;
        moment:=clock_timestamp();
        known:=EXISTS(SELECT 1 FROM jsonb_array_elements(cache.keys) k WHERE k->>'kid'=payload->>'kid')
            OR (coalesce(payload->>'scheduled'='true',false) AND cache.fetched_at IS NOT NULL);
        negative:=EXISTS(SELECT 1 FROM {schema}.identity_negative_keys WHERE issuer=cache.issuer
            AND kid=payload->>'kid' AND expires_at>moment);
        lease:=NOT negative AND (NOT known OR cache.fetched_at<=moment-interval '15 minutes')
            AND (cache.lease_until IS NULL OR cache.lease_until<=moment)
            AND (cache.attempted_at IS NULL OR cache.attempted_at<=moment-interval '60 seconds');
        IF lease THEN
            UPDATE {schema}.identity_jwks SET lease_hash=payload->>'lease_hash',lease_until=moment+interval '6 seconds',
                attempted_at=moment WHERE issuer=cache.issuer;
        END IF;
        RETURN jsonb_build_object('status','ok','refresh',lease,'inflight',cache.lease_until>moment,
            'valid_until',extract(epoch FROM cache.fetched_at+interval '1 hour'),
            'keys',CASE WHEN cache.fetched_at>moment-interval '1 hour' THEN cache.keys ELSE '[]'::jsonb END);
    ELSIF operation IN ('jwks_publish','jwks_failure') THEN
        SELECT * INTO cache FROM {schema}.identity_jwks WHERE issuer=payload->>'issuer' FOR UPDATE;
        moment:=clock_timestamp();
        IF payload->>'lease_hash' IS NULL OR payload->>'lease_hash' !~ '^[a-f0-9]{{64}}$'
           OR NOT FOUND OR cache.lease_hash IS NULL OR cache.lease_until IS NULL
           OR cache.lease_hash IS DISTINCT FROM payload->>'lease_hash' OR cache.lease_until<=moment THEN
            RETURN jsonb_build_object('status','unavailable');
        END IF;
        IF operation='jwks_publish' THEN
            IF jsonb_typeof(payload->'keys') IS DISTINCT FROM 'array' OR jsonb_array_length(payload->'keys') NOT BETWEEN 1 AND 32 THEN
                RAISE EXCEPTION 'Identity input rejected.';
            END IF;
            UPDATE {schema}.identity_jwks SET keys=payload->'keys',fetched_at=moment,lease_hash=NULL,lease_until=NULL WHERE issuer=cache.issuer;
            DELETE FROM {schema}.identity_negative_keys WHERE issuer=cache.issuer AND expires_at<=moment;
            IF length(payload->>'kid')>0 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(payload->'keys') k WHERE k->>'kid'=payload->>'kid') THEN
                IF (SELECT count(*) FROM {schema}.identity_negative_keys WHERE issuer=cache.issuer)>=128 THEN
                    DELETE FROM {schema}.identity_negative_keys WHERE (issuer,kid) IN
                        (SELECT issuer,kid FROM {schema}.identity_negative_keys WHERE issuer=cache.issuer ORDER BY expires_at LIMIT 1);
                END IF;
                INSERT INTO {schema}.identity_negative_keys VALUES(cache.issuer,payload->>'kid',moment+interval '60 seconds')
                    ON CONFLICT(issuer,kid) DO UPDATE SET expires_at=excluded.expires_at;
            END IF;
        ELSE
            UPDATE {schema}.identity_jwks SET lease_hash=NULL,lease_until=NULL WHERE issuer=cache.issuer;
        END IF;
        RETURN jsonb_build_object('status','ok');
    END IF;
    RAISE EXCEPTION 'Identity operation rejected.' USING ERRCODE='42501';
END;
$$;
REVOKE ALL ON FUNCTION {schema}.identity_call(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.identity_call(text,jsonb) TO {access_role};


-- Privilege changes invalidate a selected tenant session on its next use.
CREATE FUNCTION {schema}._identity_role_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE affected record; person uuid;
BEGIN
    FOR affected IN
        SELECT OLD.tenant_id tenant_id,OLD.membership_id membership_id WHERE TG_OP IN ('UPDATE','DELETE')
        UNION SELECT NEW.tenant_id,NEW.membership_id WHERE TG_OP IN ('INSERT','UPDATE')
    LOOP
        SELECT principal_id INTO person FROM {schema}.memberships
            WHERE tenant_id=affected.tenant_id AND id=affected.membership_id;
        PERFORM 1 FROM {schema}.principals WHERE id=person FOR UPDATE;
        UPDATE {schema}.memberships SET access_revision=access_revision+1,version=version+1,updated_at=clock_timestamp()
            WHERE tenant_id=affected.tenant_id AND id=affected.membership_id;
    END LOOP;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION {schema}._identity_role_revision() FROM PUBLIC,{access_role};
CREATE TRIGGER identity_role_revision AFTER INSERT OR UPDATE OR DELETE ON {schema}.membership_roles
    FOR EACH ROW EXECUTE FUNCTION {schema}._identity_role_revision();

-- Preserve every DB-03 privilege check; add only the exact identity capability.
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','login_transactions','identity_limits','identity_jwks','identity_negative_keys','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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

