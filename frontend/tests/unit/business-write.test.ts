// @vitest-environment node
import { expect, it, vi } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import type { LockPort } from '../../src/app/api-coordinator';
import { ApiTransport, object } from '../../src/api/transport';
import { fixture, json, requestId } from './api-fixtures';

function locks(): LockPort {
  let tail: Promise<unknown> = Promise.resolve();
  return { request: async (_name,_options,work) => { const next = tail.then(work); tail = next.catch(() => {}); return next; } };
}
const key='77777777-7777-4777-8777-777777777777';
const route='/api/v1/conversations';
function failure(status: number) { return json({ error: { code: 'conflict', message: 'Untrusted private text', retryable: false, request_id: requestId } },status); }

it('freezes body/key, uses fresh CSRF/actual binding, and decodes only after post identity', async () => {
  const order: string[]=[]; let reads=0;
  const fetch = vi.fn(async (path: string,init: RequestInit) => {
    order.push(path);
    if (path.endsWith('/me')) return json(fixture((++reads === 1 ? 'a' : 'b').repeat(43)));
    expect(init.body).toBe(JSON.stringify({ language: 'id', key }));
    expect(init.headers).toMatchObject({ 'X-CSRF-Token': 'b'.repeat(43), 'X-EAI-Expected-Context': 'a'.repeat(64) });
    return json({ id: key },201,{ 'X-EAI-Context-Binding': 'a'.repeat(64) });
  });
  const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();
  const body={ language:'id' };api.prepareWrite({ method:'POST',path:route,body },stamp,key);body.language='en';
  const decode=vi.fn((value:unknown) => { expect(order).toEqual(['/api/v1/me','/api/v1/me',route,'/api/v1/me']);return object(value); });
  expect(await api.dispatchWrite(key,stamp,decode)).toEqual({ id:key });
  expect(api.intents.current(key)).toBeUndefined();expect(decode).toHaveBeenCalledTimes(1);api.dispose();
});

it('retains a lost reply intent and replays only with explicit reconciliation using the exact key/material', async () => {
  const bodies: unknown[]=[];let writes=0;
  const fetch=vi.fn(async (path:string,init:RequestInit) => {
    if (path.endsWith('/me')) return json(fixture());
    bodies.push(init.body);if (++writes===1) throw new TypeError('private network text');
    return json({ id:key },201,{ 'X-EAI-Context-Binding':'a'.repeat(64) });
  });
  const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();
  api.prepareWrite({ method:'POST',path:route,body:{language:'en'} },stamp,key);
  await expect(api.dispatchWrite(key,stamp,object)).rejects.toMatchObject({ kind:'network',outcome:'unknown' });
  expect(api.intents.current(key)?.state).toBe('unknown');expect(writes).toBe(1);
  expect(() => api.prepareWrite({ method:'POST',path:route,body:{language:'id'} },stamp)).toThrow();
  await expect(api.dispatchWrite(key,stamp,object)).rejects.toMatchObject({ kind:'conflict' });
  expect(await api.dispatchWrite(key,stamp,object,{reconcile:true})).toEqual({id:key});
  expect(bodies).toEqual([JSON.stringify({language:'en',key}),JSON.stringify({language:'en',key})]);api.dispose();
});

it('serializes duplicate clicks and never dispatches a settled intent twice', async () => {
  const fetch=vi.fn(async (path:string) => path.endsWith('/me') ? json(fixture()) : json({id:key},201,{'X-EAI-Context-Binding':'a'.repeat(64)}));
  const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();api.prepareWrite({method:'POST',path:route,body:{language:'id'}},stamp,key);
  const first=api.dispatchWrite(key,stamp,object);const second=api.dispatchWrite(key,stamp,object).catch((e:unknown) => e);
  expect(await first).toEqual({id:key});expect(await second).toMatchObject({kind:'conflict',outcome:'not_dispatched'});
  expect(fetch.mock.calls.filter(([path]) => path===route)).toHaveLength(1);api.dispose();
});

it('known stale CAS rejection permits refresh/new intent while preserving verified identity', async () => {
  const fetch=vi.fn(async (path:string) => path.endsWith('/me') ? json(fixture()) : failure(409));
  const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();api.prepareWrite({method:'POST',path:route,body:{language:'id'}},stamp,key);
  await expect(api.dispatchWrite(key,stamp,object)).rejects.toMatchObject({kind:'conflict',outcome:'rejected'});
  expect(api.identityView(stamp).active_tenant_id).toBe(fixture().active_tenant_id);expect(api.intents.current(key)).toBeUndefined();api.dispose();
});

it('callback context drift after a successful POST discards the response and reports unknown', async () => {
  let reads=0;const decode=vi.fn();
  const fetch=vi.fn(async (path:string) => path.endsWith('/me') ? json({...fixture(),context_binding:(++reads>2?'b':'a').repeat(64)}) : json({id:key},201,{'X-EAI-Context-Binding':'a'.repeat(64)}));
  const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();api.prepareWrite({method:'POST',path:route,body:{language:'id'}},stamp,key);
  await expect(api.dispatchWrite(key,stamp,decode)).rejects.toMatchObject({kind:'context_changed',outcome:'unknown'});expect(decode).not.toHaveBeenCalled();expect(api.intents.current(key)).toBeUndefined();api.dispose();
});

it('a missing response binding or malformed success decoder never becomes a known success', async () => {
  for (const bound of [false,true]) {
    const decode=vi.fn(() => { throw new Error('private receipt contents'); });
    const fetch=vi.fn(async (path:string) => path.endsWith('/me') ? json(fixture()) : json({id:key},201,bound?{'X-EAI-Context-Binding':'a'.repeat(64)}:{}));
    const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();api.prepareWrite({method:'POST',path:route,body:{language:'id'}},stamp,key);
    await expect(api.dispatchWrite(key,stamp,decode)).rejects.toMatchObject({kind:bound?'malformed':'binding_required',outcome:'unknown'});
    expect(decode).toHaveBeenCalledTimes(bound?1:0);expect(api.intents.current(key)?.state).toBe(bound?'unknown':undefined);api.dispose();
  }
});

it('limits business paths and rejects caller-owned keys/credential fields before dispatch', async () => {
  const api=new ApiCoordinator(new ApiTransport(async () => json(fixture())),locks());const stamp=await api.bootstrap();
  for (const path of ['/api/v1/auth/logout','/api/v1/messages/x/feedback','https://evil.example/api/v1/conversations']) expect(() => api.prepareWrite({method:'POST',path,body:{}},stamp)).toThrow();
  expect(() => api.prepareWrite({method:'POST',path:route,body:{key}},stamp)).toThrow();api.dispose();
});

it('resource 404 clears only the failed resource contract when a fresh identity still matches', async () => {
  const fetch=vi.fn(async (path:string) => path.endsWith('/me') ? json(fixture()) : failure(404));const decode=vi.fn();
  const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();
  await expect(api.resourceRead({path:'/api/v1/knowledge/sources/'+key+'/content',decode},stamp)).rejects.toMatchObject({kind:'not_found'});
  expect(decode).not.toHaveBeenCalled();expect(api.identityView(stamp).active_tenant_id).toBe(fixture().active_tenant_id);api.dispose();
});

it('cancelled obsolete resource reads discard their buffer without closing the current identity', async () => {
  const controller=new AbortController();const decode=vi.fn();let entered!:()=>void;
  const started=new Promise<void>((resolve) => {entered=resolve;});
  const fetch=vi.fn(async (path:string) => {
    if (path.endsWith('/me')) return json(fixture());
    entered();return new Promise<Response>(() => {});
  });
  const api=new ApiCoordinator(new ApiTransport(fetch),locks());const stamp=await api.bootstrap();
  const read=api.resourceRead({path:'/api/v1/knowledge/documents',decode},stamp,controller.signal);
  await started;controller.abort();
  await expect(read).rejects.toMatchObject({kind:'cancelled'});
  expect(decode).not.toHaveBeenCalled();expect(api.identityView(stamp).active_tenant_id).toBe(fixture().active_tenant_id);
  api.invalidate();expect(() => api.identityView(stamp)).toThrow();api.dispose();
});

it('context invalidation during a cancelled resource read still clears identity', async () => {
  const controller=new AbortController();const decode=vi.fn();let entered!:()=>void;
  const started=new Promise<void>((resolve) => {entered=resolve;});
  const api=new ApiCoordinator(new ApiTransport(async (path:string) => {
    if (path.endsWith('/me')) return json(fixture());entered();return new Promise<Response>(() => {});
  }),locks());const stamp=await api.bootstrap();
  const read=api.resourceRead({path:'/api/v1/knowledge/documents',decode},stamp,controller.signal);
  await started;controller.abort();api.invalidate();
  await expect(read).rejects.toMatchObject({kind:'cancelled'});expect(decode).not.toHaveBeenCalled();
  expect(() => api.identityView(stamp)).toThrow();api.dispose();
});
