-- Representative owned collection/current-entry cycle; not an application feature migration.
CREATE TABLE {schema}.{parent} (
    tenant_id uuid NOT NULL, id uuid NOT NULL, owner_id uuid NOT NULL,
    current_entry_id uuid NOT NULL, version bigint NOT NULL DEFAULT 1 CHECK(version>0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(tenant_id,id),
    FOREIGN KEY(tenant_id,owner_id) REFERENCES {schema}.memberships(tenant_id,principal_id)
);
CREATE TABLE {schema}.{entry} (
    tenant_id uuid NOT NULL, id uuid NOT NULL, parent_id uuid NOT NULL,
    label text NOT NULL CHECK(length(btrim(label))>0 AND octet_length(label)<=200),
    version bigint NOT NULL DEFAULT 1 CHECK(version>0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,parent_id,id),
    FOREIGN KEY(tenant_id,parent_id) REFERENCES {schema}.{parent}(tenant_id,id)
        DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE {schema}.{parent} ADD CONSTRAINT {pointer_fk}
    FOREIGN KEY(tenant_id,id,current_entry_id) REFERENCES {schema}.{entry}(tenant_id,parent_id,id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX {owner_index} ON {schema}.{parent}(tenant_id,owner_id,id);
CREATE INDEX {parent_index} ON {schema}.{entry}(tenant_id,parent_id,id);
ALTER TABLE {schema}.{parent} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.{parent} FORCE ROW LEVEL SECURITY;
ALTER TABLE {schema}.{entry} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {schema}.{entry} FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_maintenance ON {schema}.{parent} TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY migration_maintenance ON {schema}.{entry} TO CURRENT_USER USING(true) WITH CHECK(true);
CREATE POLICY runtime_owned ON {schema}.{parent} TO {access_role}
    USING(owner_id={schema}.context_actor_id() AND {schema}.has_active_context(tenant_id))
    WITH CHECK(owner_id={schema}.context_actor_id() AND {schema}.has_active_context(tenant_id));
CREATE POLICY runtime_owned ON {schema}.{entry} TO {access_role}
    USING({schema}.has_active_context(tenant_id) AND EXISTS(SELECT 1 FROM {schema}.{parent} p
        WHERE p.tenant_id={entry}.tenant_id AND p.id={entry}.parent_id AND p.owner_id={schema}.context_actor_id()))
    WITH CHECK({schema}.has_active_context(tenant_id) AND EXISTS(SELECT 1 FROM {schema}.{parent} p
        WHERE p.tenant_id={entry}.tenant_id AND p.id={entry}.parent_id AND p.owner_id={schema}.context_actor_id()));
REVOKE ALL ON {schema}.{parent},{schema}.{entry} FROM PUBLIC,{access_role};
GRANT SELECT,INSERT ON {schema}.{parent},{schema}.{entry} TO {access_role};
GRANT UPDATE(current_entry_id,version,updated_at) ON {schema}.{parent} TO {access_role};
GRANT UPDATE(label,version,updated_at) ON {schema}.{entry} TO {access_role};
