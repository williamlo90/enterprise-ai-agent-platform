import { expect, it, vi } from 'vitest';
import { AuthController, canOpen, landing } from '../../src/app/auth-controller';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import type { LockPort } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { fixture, json, otherMembership, otherTenant } from './api-fixtures';

const locks: LockPort = { request: async (_name, _options, action) => action() };
function setup(fetch: FetchPort) { const api = new ApiCoordinator(new ApiTransport(fetch), locks); return { api, auth: new AuthController(api) }; }
it('single-flights bootstrap and exposes an immutable identity without CSRF', async () => {
  const fetch = vi.fn(async () => json(fixture())); const { api, auth } = setup(fetch);
  await Promise.all([auth.refresh(), auth.refresh()]);
  const state = auth.getSnapshot(); expect(state.kind).toBe('ready'); expect(fetch).toHaveBeenCalledTimes(1);
  expect('csrf_token' in state.identity!).toBe(false);
  expect(Object.isFrozen(state.identity!.memberships[0].roles[0])).toBe(true);
  api.invalidate(); expect(auth.getSnapshot().identity).toBeUndefined();
  expect(() => api.identityView(state.stamp!)).toThrow(); auth.dispose();
});
it('distinguishes signed-out, unavailable, no-membership and needs-tenant', async () => {
  for (const [response, expected] of [[json({}, 401), 'signed_out'], [json({}, 503), 'unavailable'],
    [json({ ...fixture(), memberships: [], active_tenant_id: null, active_membership_id: null, access_revision: null }), 'no_membership'],
    [json({ ...fixture(), active_tenant_id: null, active_membership_id: null, access_revision: null }), 'needs_tenant']] as const) {
    const { auth } = setup(async () => response); await auth.refresh(); expect(auth.getSnapshot().kind).toBe(expected); auth.dispose();
  }
});
it('keeps unsupported environments closed', async () => {
  const fetch = vi.fn(); const auth = new AuthController(new ApiCoordinator(new ApiTransport(fetch)));
  await auth.refresh(); expect(auth.getSnapshot().error).toBe('unsupported'); expect(fetch).not.toHaveBeenCalled(); auth.dispose();
});
it('does not expose a late bootstrap after context invalidation', async () => {
  let finish!: (response: Response) => void;
  const { api, auth } = setup(() => new Promise((resolve) => { finish = resolve; }));
  const load = auth.refresh(); api.invalidate(); finish(json(fixture())); await load;
  expect(auth.getSnapshot().identity).toBeUndefined(); expect(auth.getSnapshot().kind).not.toBe('ready'); auth.dispose();
});
it('resume after invalidating an in-flight bootstrap obtains a replacement without retrying an error loop', async () => {
  let finish!: (response: Response) => void; let calls = 0;
  const { api, auth } = setup(() => ++calls === 1 ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(json(fixture())));
  const original = auth.refresh(); api.invalidate();
  const resumed = auth.resume(() => true); finish(json(fixture())); await original; await resumed;
  expect(auth.getSnapshot().kind).toBe('ready'); expect(calls).toBe(2); auth.dispose();
});
it('switches with membership_id, masks old identity, and obtains the fresh rotated context', async () => {
  let current = fixture(); const bodies: string[] = [];
  const { auth } = setup(async (path, init) => {
    if (path.endsWith('/session/tenant')) { bodies.push(String(init.body)); current = { ...current, active_tenant_id: otherTenant,
      active_membership_id: otherMembership, context_binding: 'b'.repeat(64), csrf_token: 'b'.repeat(43) }; return json(current); }
    return json(current);
  });
  await auth.refresh(); const changing = auth.change({ kind: 'switch', membershipId: otherMembership });
  expect(auth.getSnapshot().kind).toBe('pending'); expect(auth.getSnapshot().identity).toBeUndefined(); await changing;
  expect(bodies).toEqual([JSON.stringify({ membership_id: otherMembership })]);
  expect(auth.getSnapshot().identity!.active_tenant_id).toBe(otherTenant); auth.dispose();
});
it('never replays an uncertain switch, and allows explicit read-only reconciliation', async () => {
  let posts = 0; const { auth } = setup(async (path) => { if (path.endsWith('/session/tenant')) { posts++; throw new Error('private upstream text'); } return json(fixture()); });
  await auth.refresh(); await auth.change({ kind: 'switch', membershipId: otherMembership });
  expect(auth.getSnapshot()).toMatchObject({ kind: 'invalid', unknown: true }); expect(auth.getSnapshot().identity).toBeUndefined();
  expect(JSON.stringify(auth.getSnapshot())).not.toContain('private upstream');
  // Lifecycle signals cannot silently replace the result-unknown message with a successful workspace.
  await auth.resume(() => true); expect(auth.getSnapshot().unknown).toBe(true);
  await auth.refresh(); expect(posts).toBe(1); expect(auth.getSnapshot().kind).toBe('ready'); auth.dispose();
});
it('claims logout only after 204 and suppresses duplicate session writes', async () => {
  let posts = 0; const { auth } = setup(async (path) => { if (path.endsWith('/logout')) { posts++; return new Response(null, { status: 204 }); } return json(fixture()); });
  await auth.refresh(); await Promise.all([auth.change({ kind: 'logout' }), auth.change({ kind: 'logout' })]);
  expect(posts).toBe(1); expect(auth.getSnapshot().kind).toBe('signed_out'); auth.dispose();
});
it('does not let a closed optional signal channel override a successful server logout', async () => {
  const api = new ApiCoordinator(new ApiTransport(async (path) => path.endsWith('/logout') ? new Response(null, { status: 204 }) : json(fixture())), locks);
  const auth = new AuthController(api, () => { throw new Error('closed optional channel'); });
  await auth.refresh(); await auth.change({ kind: 'logout' }); expect(auth.getSnapshot().kind).toBe('signed_out'); auth.dispose();
});
it('does not claim logout when the server rejects it or the result is unknown', async () => {
  for (const status of [403, 503]) {
    const { auth } = setup(async (path) => path.endsWith('/logout') ? json({}, status) : json(fixture()));
    await auth.refresh(); await auth.change({ kind: 'logout' });
    expect(auth.getSnapshot().kind).toBe('invalid'); expect(auth.getSnapshot().unknown).toBe(status === 503); auth.dispose();
  }
});
it('does not grant employee or knowledge access to admin/approver and uses current roles for landing', async () => {
  for (const [roles, path] of [[['employee'], '/chat'], [['knowledge_manager'], '/knowledge'], [['employee', 'knowledge_manager'], '/chat'], [['tenant_admin', 'approver'], '/organization']] as const) {
    const data = fixture(); data.memberships[0].roles = roles.map((role) => ({ role, scope_kind: 'tenant', scope_ref: data.active_tenant_id }));
    const { auth } = setup(async () => json(data)); await auth.refresh(); const view = auth.getSnapshot().identity!;
    expect(landing(view)).toBe(path); expect(canOpen(view, '/chat')).toBe(roles.includes('employee' as never));
    expect(canOpen(view, '/knowledge')).toBe(roles.includes('knowledge_manager' as never)); expect(canOpen(view, '/admin')).toBe(roles.includes('tenant_admin' as never)); auth.dispose();
  }
});

it('limits approval, policy and operator navigation to exact active scopes',async()=>{for(const [role,scope_kind,scope_ref,allowed,denied] of [['approver','process','reimbursement','/approvals','/admin'],['process_owner','process','reimbursement','/admin/policies/new','/admin/config'],['operator_scoped','operations','job_metadata','/operator/jobs','/operator/tenants'],['operator_scoped','operations','provisioning','/operator/tenants','/admin/config']] as const){const data=fixture();data.memberships[0].roles=[{role,scope_kind,scope_ref}];const {auth}=setup(async()=>json(data));await auth.refresh();const view=auth.getSnapshot().identity!;expect(canOpen(view,allowed)).toBe(true);expect(canOpen(view,denied)).toBe(false);auth.dispose();}});
