// @vitest-environment node
import { expect, it, vi } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import type { LockPort } from '../../src/app/api-coordinator';
import { ApiTransport, object } from '../../src/api/transport';
import { fixture, json, requestId } from './api-fixtures';

const lock: LockPort = { request: async (_name, _options, work) => work() };
const binding = 'a'.repeat(64);
const path = '/api/v1/conversations';

it('keeps a valid bound buffer internal until post identity/generation checks pass', async () => {
  const order: string[] = [];
  const fetch = vi.fn(async (target: string, init: RequestInit) => {
    order.push(target);
    if (target.endsWith('/me')) return json(fixture(String.fromCharCode(97 + order.length).repeat(43)));
    expect(init.headers).toMatchObject({ 'X-EAI-Expected-Context': binding });
    return json({ items: [] }, 200, { 'X-EAI-Context-Binding': binding });
  });
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  expect(await api.privateRead({ path, decode: object }, stamp)).toEqual({ items: [] });
  expect(order).toEqual(['/api/v1/me','/api/v1/me',path,'/api/v1/me']);
  api.dispose();
});

it.each([undefined,'b'.repeat(64)])('rejects absent/mismatched authoritative response binding before decoding (%s)', async (actual) => {
  const decode = vi.fn(object);
  const fetch = vi.fn(async (target: string) => target.endsWith('/me') ? json(fixture()) : json({ private: 'fictitious' },200,actual ? { 'X-EAI-Context-Binding': actual } : {}));
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  await expect(api.privateRead({ path, decode }, stamp)).rejects.toMatchObject({ kind: actual ? 'context_changed' : 'binding_required' });
  expect(decode).not.toHaveBeenCalled();
  expect(api.generation).toBe(1);
});

it('does not accept list B with expectation A even when surrounding identity snapshots are A', async () => {
  const decode = vi.fn(object);
  const fetch = vi.fn(async (target: string) => target.endsWith('/me') ? json(fixture()) :
    json({ error: { code: 'conflict', message: 'Request conflicts with current state.', request_id: requestId, retryable: false } },409));
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  await expect(api.privateRead({ path, decode }, stamp)).rejects.toMatchObject({ kind: 'conflict' });
  expect(decode).not.toHaveBeenCalled();
  expect((await api.bootstrap()).binding).toBe(binding);
});

it('discards a valid read buffer if the same principal/tenant returns with a new session epoch', async () => {
  let boots = 0;
  const fetch = vi.fn(async (target: string) => target.endsWith('/me') ? json({ ...fixture(), context_binding: ++boots === 3 ? 'b'.repeat(64) : binding }) : json({ items: ['fictitious'] },200,{ 'X-EAI-Context-Binding':binding }));
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  const decode = vi.fn(object);
  await expect(api.privateRead({ path, decode }, stamp)).rejects.toMatchObject({ kind: 'context_changed' });
  expect(decode).not.toHaveBeenCalled();
  expect(api.generation).toBe(1);
});

it('does not let extra structural request fields turn a bounded read into a mutation', async () => {
  const fetch = vi.fn(async (target: string, init: RequestInit) => {
    expect(init.method).toBe('GET'); expect(init.body).toBeUndefined();
    return target.endsWith('/me') ? json(fixture()) : json({ items: [] },200,{ 'X-EAI-Context-Binding':binding });
  });
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  const request = { path, decode: object, method: 'POST', body: 'not allowed', csrf: 'x'.repeat(43) };
  await api.privateRead(request,stamp);
  api.dispose();
});

it('cancels late buffers on generation invalidation and never returns private content', async () => {
  const decode = vi.fn(object);
  const fetch = vi.fn(async (target: string) => {
    if (target.endsWith('/me')) return json(fixture());
    api.invalidate();
    return json({ private: 'fictitious' },200,{ 'X-EAI-Context-Binding':binding });
  });
  const api: ApiCoordinator = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  await expect(api.privateRead({ path, decode },stamp)).rejects.toMatchObject({ kind: 'cancelled' });
  expect(decode).not.toHaveBeenCalled();
});

it('keeps binary downloads out of the JSON client and denies private reads before tenant selection', async () => {
  const fetch = vi.fn(async () => json(fixture()));
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  await expect(api.privateRead({ path: '/api/v1/files/'+requestId+'/content', decode: object },stamp)).rejects.toMatchObject({ kind:'input' });
  expect(fetch).toHaveBeenCalledTimes(1);
  api.dispose();
  const noTenant = new ApiCoordinator(new ApiTransport(async () => json({ ...fixture(), active_tenant_id:null, active_membership_id:null,access_revision:null })),lock);
  await expect(noTenant.privateRead({ path, decode:object },await noTenant.bootstrap())).rejects.toMatchObject({ kind:'forbidden' });
  noTenant.dispose();
});
