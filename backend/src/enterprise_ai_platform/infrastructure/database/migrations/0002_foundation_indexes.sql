CREATE INDEX memberships_principal_active_idx ON {schema}.memberships (principal_id, active);
CREATE INDEX membership_roles_membership_idx ON {schema}.membership_roles (tenant_id, membership_id);
CREATE INDEX membership_roles_grantor_idx ON {schema}.membership_roles (granted_by);
CREATE INDEX sessions_principal_revoked_expiry_idx ON {schema}.sessions (principal_id, revoked_at, expires_at);
CREATE INDEX sessions_active_membership_idx ON {schema}.sessions (active_tenant_id, principal_id)
    WHERE active_tenant_id IS NOT NULL;
CREATE INDEX sessions_expiry_idx ON {schema}.sessions (expires_at) WHERE revoked_at IS NULL;
CREATE INDEX security_policy_configs_approver_idx ON {schema}.security_policy_configs (approved_by)
    WHERE approved_by IS NOT NULL;
