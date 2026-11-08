// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest';
import { ApiFailure } from '../../src/api/failure';
import { ApiTransport, object, retryAfter } from '../../src/api/transport';
import { decodeIdentity } from '../../src/api/identity';
import { IntentStore } from '../../src/api/intents';
import { fixture, json, requestId } from './api-fixtures';

afterEach(() => vi.useRealTimers());

it('blocks foreign, noncanonical, traversal and login/callback paths before fetch', async () => {
  const fetch = vi.fn(); const api = new ApiTransport(fetch);
  for (const path of ['https://example.invalid/api/v1/me','//example.invalid/api/v1/me','/api/v1/../me','/api/v1/%2eme','/api/v1/auth/callback?code=secret','/api/v1/auth/login']) {
    await expect(api.request({ path, decode: object })).rejects.toMatchObject({ kind: 'input' });
  }
  expect(fetch).not.toHaveBeenCalled();
});

it('keeps cookies same-origin and redirects/errors/private payloads out of browser persistence and logs', async () => {
  const fetch = vi.fn(async () => json(fixture()));
  const api = new ApiTransport(fetch);
  const result = await api.request({ path: '/api/v1/me', bootstrap: true, decode: decodeIdentity });
  expect(result.principal_id).toBe(fixture().principal_id);
  expect(fetch.mock.calls[0]).toEqual(['/api/v1/me', expect.objectContaining({ credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer',
    headers: { Accept: 'application/json', 'X-EAI-CSRF-Bootstrap': '1' } })]);
});

it.each([[400,'input'],[401,'unauthenticated'],[403,'forbidden'],[404,'not_found'],[409,'conflict'],[429,'unavailable'],[503,'unavailable']])('maps HTTP %s without reflecting backend messages or retrying', async (status, kind) => {
  const fetch = vi.fn(async () => json({ error: { code: 'SECRET', message: '<script>SECRET</script>', request_id: requestId, retryable: true } }, status as number, { 'Retry-After': '3' }));
  const error = await new ApiTransport(fetch).request({ path: '/api/v1/me', decode: object }).catch((error: unknown) => error);
  expect(error).toBeInstanceOf(ApiFailure);
  expect(error).toMatchObject({ kind, requestId, retryAfterMs: 3000 });
  expect(String(error) + JSON.stringify(error)).not.toContain('SECRET');
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('validates response essential fields and active context consistency', () => {
  expect(decodeIdentity(fixture()).memberships.length).toBe(2);
  expect(decodeIdentity({ ...fixture(), active_tenant_id: null, active_membership_id: null, access_revision: null }).active_tenant_id).toBeNull();
  for (const value of [{ ...fixture(), csrf_token: 'bad' }, { ...fixture(), active_tenant_id: requestId },
    { ...fixture(), access_revision: 2 }, { ...fixture(), memberships: [...fixture().memberships, fixture().memberships[0]] }]) {
    expect(() => decodeIdentity(value)).toThrow(ApiFailure);
  }
});

it('refuses unexpected success status, non-JSON and malformed network JSON', async () => {
  for (const response of [json(fixture(), 202), new Response('<html>secret</html>', { headers: { 'Content-Type': 'text/html' } }),
    new Response('{bad', { headers: { 'Content-Type': 'application/json' } })]) {
    await expect(new ApiTransport(async () => response).request({ path: '/api/v1/me', decode: decodeIdentity })).rejects.toMatchObject({ kind: 'malformed' });
  }
});

it('caps streamed response memory and cancels the owned body reader', async () => {
  const cancel = vi.fn();
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array(1_048_577)); }, cancel });
  await expect(new ApiTransport(async () => new Response(body, { headers: { 'Content-Type': 'application/json' } }))
    .request({ path: '/api/v1/me', decode: object })).rejects.toMatchObject({ kind: 'malformed' });
  expect(cancel).toHaveBeenCalledTimes(1);
});

it('bounds a hanging fetch, reports unknown write outcome, and performs no auto-replay', async () => {
  vi.useFakeTimers();
  const fetch = vi.fn(() => new Promise<Response>(() => {}));
  const pending = new ApiTransport(fetch).request({ path: '/api/v1/auth/logout', method: 'POST', body: '{}', csrf: 'a'.repeat(43), decode: object }, undefined, 30)
    .catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(31);
  expect(await pending).toMatchObject({ kind: 'timeout', outcome: 'unknown' });
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('does not dispatch an already-cancelled operation or a write without valid CSRF', async () => {
  const controller = new AbortController(); controller.abort();
  const fetch = vi.fn(); const api = new ApiTransport(fetch);
  await expect(api.request({ path: '/api/v1/me', decode: object }, controller.signal)).rejects.toMatchObject({ kind: 'cancelled', outcome: 'not_dispatched' });
  await expect(api.request({ path: '/api/v1/auth/logout', method: 'POST', body: '{}', decode: object })).rejects.toMatchObject({ kind: 'input' });
  expect(fetch).not.toHaveBeenCalled();
});

it('keeps Retry-After bounded without turning it into automatic retry permission', () => {
  expect(retryAfter('900')).toBe(300_000);
  expect(retryAfter('Thu, 05 Nov 2026 00:00:03 GMT', Date.parse('Thu, 05 Nov 2026 00:00:00 GMT'))).toBe(3000);
  expect(retryAfter('secret')).toBeUndefined();
});

it('freezes intent material and key, blocks replacement after unknown, and rejects stale generations', () => {
  const store = new IntentStore(); const body = { expected_version: 1 };
  const prepared = store.prepare('fixture-operation', 2, body);
  body.expected_version = 99;
  expect(prepared.material).toBe('{"expected_version":1}');
  const running = store.transition(prepared, 'dispatching', 2);
  const unknown = store.transition(running, 'unknown', 2);
  expect(() => store.prepare('fixture-operation', 2, body)).toThrow(ApiFailure);
  const replay = store.transition(unknown, 'dispatching', 2);
  expect(replay.key).toBe(prepared.key);
  expect(replay.material).toBe(prepared.material);
  expect(() => store.transition(replay, 'settled', 3)).toThrow(ApiFailure);
  store.clear();
  expect(() => store.transition(replay, 'settled', 2)).toThrow(ApiFailure);
});

it('atomically blocks a second prepared intent from dispatching into an active or unknown resource', () => {
  const store = new IntentStore();
  const a = store.prepare('same-resource', 0, { version: 1 });
  const b = store.prepare('same-resource', 0, { version: 2 });
  const running = store.transition(a, 'dispatching', 0);
  expect(() => store.transition(b, 'dispatching', 0)).toThrow(ApiFailure);
  const unknown = store.transition(running, 'unknown', 0);
  expect(() => store.transition(b, 'dispatching', 0)).toThrow(ApiFailure);
  store.transition(unknown, 'settled', 0);
  expect(store.transition(b, 'dispatching', 0).key).toBe(b.key);
});
