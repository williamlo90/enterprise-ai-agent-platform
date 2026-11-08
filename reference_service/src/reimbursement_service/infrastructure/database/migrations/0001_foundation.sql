-- Service-local history. No platform tables, credentials or business state.
REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;
GRANT USAGE ON SCHEMA {schema} TO {runtime};
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE {schema}.runtime_binding (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    role_name name NOT NULL UNIQUE,
    role_oid oid NOT NULL UNIQUE
);
INSERT INTO {schema}.runtime_binding(role_name,role_oid)
    SELECT rolname,oid FROM pg_catalog.pg_roles WHERE rolname={runtime_name};

-- Operator-provisioned scope registry, NOT a membership/identity authority.
CREATE TABLE {schema}.tenant_registry (
    tenant_id uuid PRIMARY KEY,
    provisioning_digest text NOT NULL CHECK(provisioning_digest ~ '^[a-f0-9]{{64}}$'),
    active boolean NOT NULL DEFAULT false,
    provisioned_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE {schema}.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.schema_migrations FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runtime_binding ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runtime_binding FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenant_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenant_registry FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON {schema}.schema_migrations TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY binding_owner ON {schema}.runtime_binding TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY registry_owner ON {schema}.tenant_registry TO CURRENT_USER USING(true) WITH CHECK(true);
REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC, {runtime};
GRANT SELECT ON {schema}.tenant_registry TO {runtime};

CREATE FUNCTION {schema}.context_tenant() RETURNS uuid
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $fn$
BEGIN
    RETURN nullif(current_setting('reimbursement.tenant_id',true),'')::uuid;
EXCEPTION WHEN invalid_text_representation THEN RETURN NULL;
END $fn$;
CREATE FUNCTION {schema}.context_actor() RETURNS uuid
LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $fn$
BEGIN
    RETURN nullif(current_setting('reimbursement.actor_id',true),'')::uuid;
EXCEPTION WHEN invalid_text_representation THEN RETURN NULL;
END $fn$;

CREATE FUNCTION {schema}.runtime_ready() RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE r record; n oid; c record; p record; privilege text;
BEGIN
    SELECT roles.* INTO r FROM pg_catalog.pg_roles roles
      JOIN {schema}.runtime_binding b ON b.role_oid=roles.oid AND b.role_name=roles.rolname
      WHERE roles.rolname=SESSION_USER;
    IF NOT FOUND OR NOT r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreatedb
        OR r.rolcreaterole OR r.rolreplication OR r.rolinherit THEN RETURN false; END IF;
    IF EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=r.oid) THEN RETURN false; END IF;
    SELECT oid INTO n FROM pg_catalog.pg_namespace WHERE nspname={schema_name};
    IF NOT has_schema_privilege(r.oid,n,'USAGE') OR has_schema_privilege(r.oid,n,'CREATE')
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspowner=r.oid)
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE relowner=r.oid)
      OR EXISTS(SELECT 1 FROM pg_catalog.pg_proc WHERE proowner=r.oid) THEN RETURN false; END IF;
    IF (SELECT count(*) FROM {schema}.schema_migrations)<>1 OR NOT EXISTS(
        SELECT 1 FROM {schema}.schema_migrations WHERE version=1 AND name='0001_foundation.sql'
    ) THEN RETURN false; END IF;
    FOR c IN SELECT * FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind IN ('r','p','v','m','S','f') LOOP
        IF c.relkind<>'r' OR c.relname NOT IN ('schema_migrations','runtime_binding','tenant_registry')
          OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity THEN RETURN false; END IF;
        IF has_table_privilege(r.oid,c.oid,'SELECT')<>(c.relname='tenant_registry') THEN RETURN false; END IF;
        FOREACH privilege IN ARRAY ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','SELECT WITH GRANT OPTION'] LOOP
            IF has_table_privilege(r.oid,c.oid,privilege) THEN RETURN false; END IF;
        END LOOP;
        FOREACH privilege IN ARRAY ARRAY['INSERT','UPDATE','REFERENCES','SELECT WITH GRANT OPTION'] LOOP
            IF has_any_column_privilege(r.oid,c.oid,privilege) THEN RETURN false; END IF;
        END LOOP;
        IF c.relname<>'tenant_registry' AND has_any_column_privilege(r.oid,c.oid,'SELECT') THEN RETURN false; END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace=n AND relkind='r')<>3 THEN RETURN false; END IF;
    FOR p IN SELECT * FROM pg_catalog.pg_proc WHERE pronamespace=n LOOP
        IF p.proname NOT IN ('context_tenant','context_actor','runtime_ready') OR p.pronargs<>0
          OR NOT has_function_privilege(r.oid,p.oid,'EXECUTE')
          OR has_function_privilege(r.oid,p.oid,'EXECUTE WITH GRANT OPTION') THEN RETURN false; END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace=n)<>3 THEN RETURN false; END IF;
    RETURN true;
END $fn$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM PUBLIC, {runtime};
GRANT EXECUTE ON FUNCTION {schema}.context_tenant(), {schema}.context_actor(), {schema}.runtime_ready() TO {runtime};
CREATE POLICY tenant_scope ON {schema}.tenant_registry FOR SELECT TO {runtime}
    USING(active AND tenant_id={schema}.context_tenant()
          AND {schema}.context_actor() IS NOT NULL
          AND {schema}.context_actor()<>'00000000-0000-0000-0000-000000000000'::uuid
          AND {schema}.runtime_ready());
