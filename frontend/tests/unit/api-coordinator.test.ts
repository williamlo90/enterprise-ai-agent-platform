// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest';
import { ApiCoordinator, sessionLockName } from '../../src/app/api-coordinator';
import type { LockPort } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import { fixture, json, otherMembership, otherTenant, person } from './api-fixtures';

afterEach(() => vi.useRealTimers());
it.each(['/api/v1/organization/members','/api/v1/organization/facets?field=group','/api/v1/knowledge/collections'])(
  'reads the rolled-out directory path %s only with server binding and fresh identity checks', async (path) => {
    const decode = vi.fn((value: unknown) => value);
    const fetch = vi.fn(async (target: string, init: RequestInit) => {
      if (target === '/api/v1/me') return json(fixture());
      expect(init.headers).toMatchObject({ 'X-EAI-Expected-Context': 'a'.repeat(64) });
      return json({ items: [] }, 200, { 'X-EAI-Context-Binding': 'a'.repeat(64) });
    });
    const api = new ApiCoordinator(new ApiTransport(fetch), locks());
    const stamp = await api.bootstrap();
    expect(await api.privateRead({ path, decode }, stamp)).toEqual({ items: [] });
    expect(fetch.mock.calls.map(([target]) => target)).toEqual(['/api/v1/me','/api/v1/me',path,'/api/v1/me']);
    expect(decode).toHaveBeenCalledTimes(1); api.dispose();
  });
function locks(): LockPort {
  let tail: Promise<unknown> = Promise.resolve();
  const port: LockPort = { async request<T>(_name: string, _options: { mode: 'exclusive'; signal: AbortSignal }, callback: () => Promise<T>) {
    const work = tail.then(callback); tail = work.catch(() => {}); return work;
  } };
  vi.spyOn(port, 'request'); return port;
}

it('single-flights concurrent bootstrap callers under one constant exclusive lock', async () => {
  const lock = locks(); const fetch = vi.fn(async () => json(fixture()));
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const first = api.bootstrap(); const second = api.bootstrap();
  expect(first).toBe(second);
  const stamp = await first;
  expect(stamp.generation).toBe(0);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(lock.request).toHaveBeenCalledWith(sessionLockName, expect.objectContaining({ mode: 'exclusive' }), expect.any(Function));
  api.dispose();
});

it('bootstraps a fresh CSRF and sends logout {} inside the same lock, then clears context', async () => {
  const seen: string[] = []; let holding = false;
  const lock: LockPort = { async request(_name, _options, work) { holding = true; try { return await work(); } finally { holding = false; } } };
  const fetch = vi.fn(async (path: string, init: RequestInit) => {
    expect(holding).toBe(true); seen.push(path);
    if (path.endsWith('/me')) return json(fixture(seen.length === 1 ? 'a'.repeat(43) : 'b'.repeat(43)));
    expect(init.body).toBe('{}');
    expect(init.headers).toMatchObject({ 'X-CSRF-Token': 'b'.repeat(43) });
    return new Response(null, { status: 204 });
  });
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  await api.changeSession({ kind: 'logout' }, stamp);
  expect(seen).toEqual(['/api/v1/me','/api/v1/me','/api/v1/auth/logout']);
  expect(api.generation).toBe(1);
  await expect(api.changeSession({ kind: 'logout' }, stamp)).rejects.toMatchObject({ kind: 'context_changed' });
});

it('sends membership_id, validates the switched principal/tenant, and masks before reconciliation', async () => {
  const fetch = vi.fn(async (path: string, init: RequestInit) => {
    if (path.endsWith('/me')) return json(fixture());
    expect(init.body).toBe(JSON.stringify({ membership_id: otherMembership }));
    return json({ ...fixture(), principal_id: person, active_membership_id: otherMembership, active_tenant_id: otherTenant, context_binding: 'b'.repeat(64) });
  });
  const api = new ApiCoordinator(new ApiTransport(fetch), locks());
  const stamp = await api.bootstrap();
  await api.changeSession({ kind: 'switch', membershipId: otherMembership }, stamp);
  expect(api.generation).toBe(1);
});

it('cancels queued intents when context changes before the lock is acquired', async () => {
  const lock = locks(); const fetch = vi.fn(async () => json(fixture()));
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const stamp = await api.bootstrap();
  const pending = api.changeSession({ kind: 'logout' }, stamp).catch((error: unknown) => error);
  api.invalidate();
  expect(await pending).toMatchObject({ kind: 'cancelled', outcome: 'not_dispatched' });
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('rejects identity drift before dispatch instead of silently adopting a different context', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(json(fixture())).mockResolvedValueOnce(json({ ...fixture(), access_revision: 2,
    memberships: fixture().memberships.map((item) => ({ ...item, access_revision: 2 })) }));
  const api = new ApiCoordinator(new ApiTransport(fetch), locks());
  const stamp = await api.bootstrap();
  await expect(api.changeSession({ kind: 'logout' }, stamp)).rejects.toMatchObject({ kind: 'context_changed', outcome: 'not_dispatched' });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it('does not let an aborted old bootstrap invalidate a newer bootstrap generation', async () => {
  const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(() => {})).mockImplementationOnce(async () => json(fixture()));
  // This test focuses the stale-completion guard, not actual Web Locks implementation.
  const lock: LockPort = { request: async (_name, _options, work) => work() };
  const api = new ApiCoordinator(new ApiTransport(fetch), lock);
  const old = api.bootstrap().catch((error: unknown) => error);
  api.invalidate();
  const fresh = api.bootstrap();
  expect(await old).toMatchObject({ kind: 'cancelled' });
  expect((await fresh).generation).toBe(1);
  expect(api.generation).toBe(1);
  api.dispose();
});

it('marks a lost logout response unknown and never automatically replays it', async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(async (path: string) => path.endsWith('/me') ? json(fixture()) : new Promise<Response>(() => {}));
  const api = new ApiCoordinator(new ApiTransport(fetch), locks(), 30);
  const stamp = await api.bootstrap();
  const pending = api.changeSession({ kind: 'logout' }, stamp).catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(31);
  expect(await pending).toMatchObject({ outcome: 'unknown' });
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(api.generation).toBe(1);
});

it('blocks legacy bootstrap without a server context binding before any private read', async () => {
  const fetch = vi.fn(async () => json({ ...fixture(), context_binding: undefined })); const api = new ApiCoordinator(new ApiTransport(fetch), locks());
  await expect(api.bootstrap()).rejects.toMatchObject({ kind: 'binding_required' });
  expect(fetch).toHaveBeenCalledTimes(1);
  api.dispose();
});

it('fails closed without Web Locks and bounds an unresponsive lock queue', async () => {
  const fetch = vi.fn();
  await expect(new ApiCoordinator(new ApiTransport(fetch)).bootstrap()).rejects.toMatchObject({ kind: 'unsupported' });
  vi.useFakeTimers();
  const lock: LockPort = { request: () => new Promise(() => {}) };
  const api = new ApiCoordinator(new ApiTransport(fetch), lock, 25);
  const pending = api.bootstrap().catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(26);
  expect(await pending).toMatchObject({ kind: 'timeout' });
  expect(fetch).not.toHaveBeenCalled();
});

it('normalizes native AbortError from a cancelled or timed-out Web Lock queue', async () => {
  const lock: LockPort = { request: (_name, options) => new Promise((_, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('sensitive native detail', 'AbortError')), { once: true });
  }) };
  const fetch = vi.fn();
  const api = new ApiCoordinator(new ApiTransport(fetch), lock, 25);
  const cancelled = api.bootstrap().catch((error: unknown) => error);
  api.invalidate();
  expect(await cancelled).toMatchObject({ kind: 'cancelled' });
  vi.useFakeTimers();
  const timedOut = api.bootstrap().catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(26);
  expect(await timedOut).toMatchObject({ kind: 'timeout' });
  expect(fetch).not.toHaveBeenCalled();
});
