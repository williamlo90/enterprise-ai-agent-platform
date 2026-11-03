CREATE ROLE {access_role} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;
CREATE TABLE {schema}.runtime_access_role_registry (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    role_name name NOT NULL UNIQUE,
    role_oid oid NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO {schema}.runtime_access_role_registry(role_name,role_oid)
    SELECT rolname,oid FROM pg_catalog.pg_roles WHERE oid='{access_role}'::regrole;

CREATE TABLE {schema}.runtime_role_registry (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    role_name name NOT NULL UNIQUE,
    role_oid oid NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE FUNCTION {schema}.context_tenant_id() RETURNS uuid
LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE value text;
BEGIN
    value := current_setting('app.tenant_id', true);
    IF value IS NULL OR value !~ '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$' THEN
        RETURN NULL;
    END IF;
    RETURN value::uuid;
END;
$$;
CREATE FUNCTION {schema}.context_actor_id() RETURNS uuid
LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE value text;
BEGIN
    value := current_setting('app.actor_id', true);
    IF value IS NULL OR value !~ '^[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}$' THEN
        RETURN NULL;
    END IF;
    RETURN value::uuid;
END;
$$;

CREATE FUNCTION {schema}.has_active_context(target_tenant uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT target_tenant = {schema}.context_tenant_id()
       AND {schema}.context_actor_id() IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM {schema}.memberships m
           JOIN {schema}.principals p ON p.id = m.principal_id
           JOIN {schema}.tenants t ON t.id = m.tenant_id
           WHERE m.tenant_id = target_tenant AND m.principal_id = {schema}.context_actor_id()
             AND m.active AND p.active AND t.active
       );
$$;

CREATE FUNCTION {schema}._runtime_role_is_valid(candidate oid) RETURNS boolean
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
                AND c.relname IN ('principals','tenants','memberships','membership_roles','sessions',
                    'tenant_configs','security_policy_configs','timezone_catalog','schema_migrations','runtime_role_registry','runtime_access_role_registry')
                AND (has_table_privilege(r.oid,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(r.oid,c.oid,'INSERT,UPDATE,REFERENCES'))
          )
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_class c
              WHERE c.relnamespace = (SELECT relnamespace FROM pg_catalog.pg_class
                  WHERE oid='{schema}.runtime_role_registry'::regclass)
                AND c.relname IN ('sessions','security_policy_configs','schema_migrations','runtime_role_registry','runtime_access_role_registry')
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

CREATE FUNCTION {schema}.runtime_role_is_valid() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT {schema}._runtime_role_is_valid((SELECT oid FROM pg_catalog.pg_roles WHERE rolname=SESSION_USER));
$$;

ALTER TABLE {schema}.principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.principals FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.membership_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.membership_roles FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenant_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.tenant_configs FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.security_policy_configs ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.security_policy_configs FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.schema_migrations FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runtime_role_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runtime_role_registry FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runtime_access_role_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.runtime_access_role_registry FORCE ROW LEVEL SECURITY;

-- Explicit maintenance authority belongs to the migration identity, never the
-- runtime login. The definer helper performs only a boolean membership check.
CREATE POLICY migration_maintenance ON {schema}.principals TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.tenants TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.memberships TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.membership_roles TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.tenant_configs TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.security_policy_configs TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.sessions TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.schema_migrations TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.runtime_role_registry TO CURRENT_USER USING (true) WITH CHECK (true);
CREATE POLICY migration_maintenance ON {schema}.runtime_access_role_registry TO CURRENT_USER USING (true) WITH CHECK (true);

CREATE POLICY runtime_self ON {schema}.principals FOR SELECT TO {access_role}
    USING (id={schema}.context_actor_id() AND {schema}.has_active_context({schema}.context_tenant_id()));
CREATE POLICY runtime_tenant ON {schema}.tenants FOR SELECT TO {access_role}
    USING ({schema}.has_active_context(id));
CREATE POLICY runtime_self ON {schema}.memberships FOR SELECT TO {access_role}
    USING (principal_id={schema}.context_actor_id() AND {schema}.has_active_context(tenant_id));
CREATE POLICY runtime_self ON {schema}.membership_roles FOR SELECT TO {access_role}
    USING ({schema}.has_active_context(tenant_id) AND EXISTS (
        SELECT 1 FROM {schema}.memberships m WHERE m.tenant_id=membership_roles.tenant_id
            AND m.id=membership_roles.membership_id AND m.principal_id={schema}.context_actor_id()));
CREATE POLICY runtime_tenant ON {schema}.tenant_configs FOR SELECT TO {access_role}
    USING ({schema}.has_active_context(tenant_id));

REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM PUBLIC;
GRANT USAGE ON SCHEMA {schema} TO {access_role};
GRANT SELECT ON {schema}.principals,{schema}.tenants,{schema}.memberships,
    {schema}.membership_roles,{schema}.tenant_configs,{schema}.timezone_catalog TO {access_role};
GRANT EXECUTE ON FUNCTION {schema}.context_tenant_id(),{schema}.context_actor_id(),
    {schema}.has_active_context(uuid),{schema}.runtime_role_is_valid() TO {access_role};
