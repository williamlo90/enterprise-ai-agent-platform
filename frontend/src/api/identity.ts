import { ApiFailure } from './failure';
import { isUuid, object } from './transport';

export interface RoleGrant { role: string; scope_kind: string; scope_ref: string }
export interface Membership { membership_id: string; tenant_id: string; tenant_name: string; access_revision: number; roles: RoleGrant[] }
export interface Identity {
  principal_id: string; active_tenant_id: string | null; active_membership_id: string | null;
  access_revision: number | null; memberships: Membership[]; csrf_token: string; context_binding: string;
}
export type IdentityView = Readonly<Omit<Identity, 'csrf_token' | 'memberships'> & {
  memberships: readonly Readonly<Omit<Membership, 'roles'> & { roles: readonly Readonly<RoleGrant>[] }>[];
}>;
export function projectIdentity(identity: Identity): IdentityView {
  return Object.freeze({ principal_id: identity.principal_id, active_tenant_id: identity.active_tenant_id,
    active_membership_id: identity.active_membership_id, access_revision: identity.access_revision,
    context_binding: identity.context_binding,
    memberships: Object.freeze(identity.memberships.map((item) => Object.freeze({ ...item,
      roles: Object.freeze(item.roles.map((grant) => Object.freeze({ ...grant }))) }))) });
}
const roles = new Set(['employee','knowledge_manager','tenant_admin','process_owner','approver','operator_scoped']);
function revision(value: unknown): value is number { return Number.isSafeInteger(value) && (value as number) >= 0; }
function text(value: unknown, maximum: number): value is string { return typeof value === 'string' && value.length > 0 && value.length <= maximum; }

export function decodeIdentity(value: unknown): Identity {
  const data = object(value);
  if (typeof data.context_binding !== 'string' || !/^[a-f0-9]{64}$/.test(data.context_binding)) throw new ApiFailure('binding_required');
  if (!isUuid(data.principal_id) || !text(data.csrf_token, 43) || !/^[A-Za-z0-9_-]{43}$/.test(data.csrf_token) || !Array.isArray(data.memberships)) throw new ApiFailure('malformed');
  const memberships = data.memberships.map((value): Membership => {
    const item = object(value);
    if (!isUuid(item.membership_id) || !isUuid(item.tenant_id) || !text(item.tenant_name, 240) || !revision(item.access_revision) || !Array.isArray(item.roles)) throw new ApiFailure('malformed');
    const grants = item.roles.map((value): RoleGrant => {
      const grant = object(value);
      if (typeof grant.role !== 'string' || !roles.has(grant.role) || !text(grant.scope_kind, 64) || !text(grant.scope_ref, 128)) throw new ApiFailure('malformed');
      return { role: grant.role, scope_kind: grant.scope_kind, scope_ref: grant.scope_ref };
    });
    return { membership_id: item.membership_id, tenant_id: item.tenant_id, tenant_name: item.tenant_name, access_revision: item.access_revision, roles: grants };
  });
  if (new Set(memberships.map((item) => item.membership_id)).size !== memberships.length) throw new ApiFailure('malformed');
  if (data.active_tenant_id === null && data.active_membership_id === null && data.access_revision === null) {
    return { principal_id: data.principal_id, active_tenant_id: null, active_membership_id: null, access_revision: null, memberships, csrf_token: data.csrf_token, context_binding: data.context_binding };
  }
  if (!isUuid(data.active_tenant_id) || !isUuid(data.active_membership_id) || !revision(data.access_revision) ||
      !memberships.some((item) => item.membership_id === data.active_membership_id && item.tenant_id === data.active_tenant_id && item.access_revision === data.access_revision)) throw new ApiFailure('malformed');
  return { principal_id: data.principal_id, active_tenant_id: data.active_tenant_id, active_membership_id: data.active_membership_id,
    access_revision: data.access_revision, memberships, csrf_token: data.csrf_token, context_binding: data.context_binding };
}

export function identityFingerprint(identity: Identity): string {
  return JSON.stringify([identity.context_binding, identity.principal_id, identity.active_tenant_id, identity.active_membership_id, identity.access_revision,
    identity.memberships.map((item) => [item.membership_id,item.tenant_id,item.access_revision,
      item.roles.map((grant) => JSON.stringify([grant.role,grant.scope_kind,grant.scope_ref])).sort()]).sort((a,b) => String(a[0]).localeCompare(String(b[0]))) ]);
}
