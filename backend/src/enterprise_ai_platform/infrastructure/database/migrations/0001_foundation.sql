CREATE TABLE {schema}.timezone_catalog (
    name text PRIMARY KEY,
    catalog_version integer NOT NULL CHECK (catalog_version > 0)
);
INSERT INTO {schema}.timezone_catalog (name, catalog_version)
VALUES ('Asia/Jakarta', 1), ('Asia/Singapore', 1), ('UTC', 1);

CREATE TABLE {schema}.principals (
    id uuid PRIMARY KEY,
    issuer text NOT NULL CHECK (length(btrim(issuer)) BETWEEN 1 AND 2048 AND octet_length(issuer) <= 2048),
    subject text NOT NULL CHECK (length(btrim(subject)) BETWEEN 1 AND 512 AND octet_length(subject) <= 512),
    display_name text CHECK (display_name IS NULL OR length(btrim(display_name)) BETWEEN 1 AND 200),
    active boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT principals_identity_unique UNIQUE (issuer, subject)
);

CREATE TABLE {schema}.tenants (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(slug) <= 80),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    timezone text NOT NULL REFERENCES {schema}.timezone_catalog(name) ON DELETE RESTRICT,
    active boolean NOT NULL DEFAULT true,
    config_version bigint NOT NULL DEFAULT 1 CHECK (config_version > 0),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE {schema}.memberships (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id) ON DELETE RESTRICT,
    id uuid NOT NULL,
    principal_id uuid NOT NULL REFERENCES {schema}.principals(id) ON DELETE RESTRICT,
    active boolean NOT NULL DEFAULT true,
    access_revision bigint NOT NULL DEFAULT 1 CHECK (access_revision > 0),
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, principal_id),
    UNIQUE (tenant_id, id, principal_id)
);

CREATE TABLE {schema}.membership_roles (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id) ON DELETE RESTRICT,
    id uuid NOT NULL,
    membership_id uuid NOT NULL,
    role text NOT NULL CHECK (role IN ('employee', 'knowledge_manager', 'tenant_admin',
        'process_owner', 'approver', 'operator_scoped')),
    scope_kind text NOT NULL CHECK (scope_kind ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    scope_ref text NOT NULL CHECK (length(btrim(scope_ref)) BETWEEN 1 AND 200),
    granted_by uuid NOT NULL REFERENCES {schema}.principals(id) ON DELETE RESTRICT,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, membership_id) REFERENCES {schema}.memberships(tenant_id, id) ON DELETE RESTRICT,
    UNIQUE (tenant_id, membership_id, role, scope_kind, scope_ref)
);

CREATE TABLE {schema}.sessions (
    id uuid PRIMARY KEY,
    principal_id uuid NOT NULL REFERENCES {schema}.principals(id) ON DELETE RESTRICT,
    token_hash text NOT NULL UNIQUE CHECK (length(btrim(token_hash)) BETWEEN 1 AND 512),
    csrf_hash text NOT NULL CHECK (length(btrim(csrf_hash)) BETWEEN 1 AND 512),
    active_tenant_id uuid,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (expires_at > created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY (active_tenant_id, principal_id)
        REFERENCES {schema}.memberships(tenant_id, principal_id) ON DELETE RESTRICT
);

CREATE TABLE {schema}.tenant_configs (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id) ON DELETE RESTRICT,
    id uuid NOT NULL,
    config_version bigint NOT NULL CHECK (config_version > 0),
    assistant_name text NOT NULL CHECK (length(btrim(assistant_name)) BETWEEN 1 AND 200),
    locales jsonb NOT NULL CHECK (locales IN ('["id"]'::jsonb, '["en"]'::jsonb,
                                            '["id","en"]'::jsonb, '["en","id"]'::jsonb)),
    help_contact text NOT NULL CHECK (length(btrim(help_contact)) BETWEEN 1 AND 500),
    instruction_text text NOT NULL CHECK (length(btrim(instruction_text)) BETWEEN 1 AND 16000),
    connector_ref text NOT NULL CHECK (connector_ref ~ '^[a-z][a-z0-9_.-]{{0,127}}$'),
    model_policy_ref text NOT NULL CHECK (model_policy_ref ~ '^[a-z][a-z0-9_.-]{{0,127}}$'),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, config_version)
);

ALTER TABLE {schema}.tenants ADD CONSTRAINT tenants_config_snapshot_fk
    FOREIGN KEY (id, config_version) REFERENCES {schema}.tenant_configs(tenant_id, config_version)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE {schema}.security_policy_configs (
    tenant_id uuid NOT NULL REFERENCES {schema}.tenants(id) ON DELETE RESTRICT,
    id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    category text NOT NULL CHECK (category ~ '^[a-z][a-z0-9_]{{0,63}}$'),
    retention_seconds bigint CHECK (retention_seconds IS NULL OR retention_seconds > 0),
    region text CHECK (region IS NULL OR length(btrim(region)) BETWEEN 1 AND 64),
    processing_policy jsonb NOT NULL CHECK (jsonb_typeof(processing_policy) = 'object'),
    schema_version integer NOT NULL CHECK (schema_version = 1),
    approved_by uuid REFERENCES {schema}.principals(id) ON DELETE RESTRICT,
    approved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CHECK (approved_at IS NULL OR (retention_seconds IS NOT NULL AND region IS NOT NULL)),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, revision, category)
);

CREATE FUNCTION {schema}.reject_snapshot_mutation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'Immutable configuration snapshot.' USING ERRCODE = '55000';
END;
$$;
CREATE TRIGGER tenant_configs_immutable BEFORE UPDATE OR DELETE ON {schema}.tenant_configs
    FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER tenant_configs_no_truncate BEFORE TRUNCATE ON {schema}.tenant_configs
    FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER security_policy_configs_immutable BEFORE UPDATE OR DELETE ON {schema}.security_policy_configs
    FOR EACH ROW EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
CREATE TRIGGER security_policy_configs_no_truncate BEFORE TRUNCATE ON {schema}.security_policy_configs
    FOR EACH STATEMENT EXECUTE FUNCTION {schema}.reject_snapshot_mutation();
