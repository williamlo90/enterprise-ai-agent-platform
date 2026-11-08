export const person = '11111111-1111-4111-8111-111111111111';
export const tenant = '22222222-2222-4222-8222-222222222222';
export const membership = '33333333-3333-4333-8333-333333333333';
export const otherTenant = '44444444-4444-4444-8444-444444444444';
export const otherMembership = '55555555-5555-4555-8555-555555555555';
export const requestId = '66666666-6666-4666-8666-666666666666';
export function fixture(csrf = 'a'.repeat(43)) {
  return { principal_id: person, active_tenant_id: tenant, active_membership_id: membership, access_revision: 1,
    memberships: [{ membership_id: membership, tenant_id: tenant, tenant_name: 'Fiktif', access_revision: 1,
      roles: [{ role: 'employee', scope_kind: 'tenant', scope_ref: tenant }] },
    { membership_id: otherMembership, tenant_id: otherTenant, tenant_name: 'Fiktif lain', access_revision: 1, roles: [] }], csrf_token: csrf, context_binding: 'a'.repeat(64) };
}
export function json(value: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}
