// @vitest-environment node
import { expect, it, vi } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import type { LockPort } from '../../src/app/api-coordinator';
import { ApiTransport, object } from '../../src/api/transport';
import { maximumUploadBytes } from '../../src/api/upload-policy';
import { fixture, json, requestId } from './api-fixtures';

const key='77777777-7777-4777-8777-777777777777';
const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const locks:LockPort={request:async (_name,_options,work) => work()};

it('snapshots upload metadata before hashing yields to the caller',async () => {
  const api=new ApiCoordinator(new ApiTransport(async () => json(fixture())),locks);const stamp=await api.bootstrap();
  const request:{file:Blob;mime:'text/plain'|'application/pdf';collectionId:string|null}={file:new Blob(['original']),mime:'text/plain',collectionId:null};
  const pending=api.prepareUpload(request,stamp,key);
  request.file=new Blob(['replacement']);request.mime='application/pdf';request.collectionId=key;
  const intent=await pending;expect(JSON.parse(intent.material)).toMatchObject({mime:'text/plain',collection_id:null,size:8,path:'/api/v1/files?purpose=knowledge'});
  api.discardPrepared(key);api.dispose();
});

it('holds one immutable bounded blob and reuses exactly the same bytes/key after a lost upload reply',async () => {
  const sent:Blob[]=[];let writes=0;
  const api=new ApiCoordinator(new ApiTransport(async (path,init) => {
    if (path.endsWith('/me')) return json(fixture());
    expect(path).toBe('/api/v1/files?purpose=knowledge');
    expect(init.headers).toMatchObject({'Content-Type':'text/plain','Idempotency-Key':key,'X-EAI-Expected-Context':'a'.repeat(64)});
    sent.push(init.body as Blob);if (++writes===1) throw new TypeError('private body');
    return json({id:key},202,binding);
  }),locks);const stamp=await api.bootstrap();
  const intent=await api.prepareUpload({file:new Blob(['hello']),mime:'text/plain'},stamp,key);
  expect(JSON.parse(intent.material)).toMatchObject({size:5,digest:'2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'});
  expect(intent.material).not.toContain('hello');
  await expect(api.prepareUpload({file:new Blob(['other']),mime:'text/plain'},stamp)).rejects.toMatchObject({kind:'conflict'});
  await expect(api.dispatchWrite(key,stamp,object)).rejects.toMatchObject({outcome:'unknown'});
  expect(() => api.discardPrepared(key)).toThrow();expect(writes).toBe(1);
  expect(await api.dispatchWrite(key,stamp,object,{reconcile:true})).toEqual({id:key});
  expect(sent[0]).toBe(sent[1]);expect(await sent[1].text()).toBe('hello');
  const next=await api.prepareUpload({file:new Blob(['next']),mime:'text/plain'},stamp);api.discardPrepared(next.key);api.dispose();
});

it('keeps partial upload conflict unknown but releases held bytes after a known input rejection',async () => {
  for (const status of [409,413]) {
    const api=new ApiCoordinator(new ApiTransport(async (path) => path.endsWith('/me')?json(fixture()):json({error:{code:'conflict',message:'private',retryable:false,request_id:requestId}},status)),locks);
    const stamp=await api.bootstrap();await api.prepareUpload({file:new Blob(['hello']),mime:'text/plain'},stamp,key);
    await expect(api.dispatchWrite(key,stamp,object)).rejects.toMatchObject({outcome:status===409?'unknown':'rejected'});
    expect(api.intents.current(key)?.state).toBe(status===409?'unknown':undefined);
    if (status===413) {const next=await api.prepareUpload({file:new Blob(['next']),mime:'text/plain'},stamp);api.discardPrepared(next.key);}
    api.dispose();
  }
});

it('rejects empty/oversize/unsupported files and does not retain prepared bytes across invalidation',async () => {
  const fetch=vi.fn(async () => json(fixture()));const api=new ApiCoordinator(new ApiTransport(fetch),locks);const stamp=await api.bootstrap();
  for (const file of [new Blob([]),new Blob([new Uint8Array(maximumUploadBytes+1)])]) await expect(api.prepareUpload({file,mime:'text/plain'},stamp)).rejects.toMatchObject({kind:'input'});
  const controller=new AbortController();controller.abort();
  await expect(api.prepareUpload({file:new Blob(['x']),mime:'text/plain'},stamp,undefined,controller.signal)).rejects.toMatchObject({kind:'cancelled'});
  await api.prepareUpload({file:new Blob(['x']),mime:'text/plain'},stamp,key);api.invalidate();
  await expect(api.dispatchWrite(key,stamp,object)).rejects.toMatchObject({kind:'context_changed'});expect(fetch).toHaveBeenCalledTimes(1);api.dispose();
});

it('sends Markdown as explicit raw text/markdown with context and stable key',async () => {
  const api=new ApiCoordinator(new ApiTransport(async (path,init) => {
    if (path.endsWith('/me')) return json(fixture());
    expect(init.headers).toMatchObject({'Content-Type':'text/markdown','Idempotency-Key':key});
    expect(await (init.body as Blob).text()).toBe('# Policy\nUTF-8: ü');return json({id:key},202,binding);
  }),locks);const stamp=await api.bootstrap();
  await api.prepareUpload({file:new Blob(['# Policy\nUTF-8: ü']),mime:'text/markdown'},stamp,key);
  expect(api.uploadIntent(stamp)?.key).toBe(key);await api.dispatchWrite(key,stamp,object);expect(api.uploadIntent(stamp)).toBeUndefined();api.dispose();
});

it('does not resurrect bytes or intent when cancellation or context invalidation happens during hashing',async () => {
  for (const invalidate of [false,true]) {
    const api=new ApiCoordinator(new ApiTransport(async () => json(fixture())),locks);const stamp=await api.bootstrap();
    let release!:(value:ArrayBuffer)=>void;const hash=vi.spyOn(crypto.subtle,'digest').mockImplementationOnce(() => new Promise((resolve) => {release=resolve;}));
    const controller=new AbortController();const pending=api.prepareUpload({file:new Blob(['private']),mime:'text/markdown'},stamp,key,controller.signal);
    await vi.waitFor(() => expect(hash).toHaveBeenCalled());
    if (invalidate) api.invalidate();else controller.abort();release(new Uint8Array(32).buffer);
    await expect(pending).rejects.toMatchObject({kind:invalidate?'context_changed':'cancelled'});expect(api.uploadIntent(stamp)).toBeUndefined();expect(api.intents.current(key)).toBeUndefined();hash.mockRestore();api.dispose();
  }
});

it('snapshots receipt purpose before hashing and permits image MIME only for receipts',async()=>{const bodies:RequestInit[]=[];const api=new ApiCoordinator(new ApiTransport(async(path,init)=>{if(path.endsWith('/me'))return json(fixture());bodies.push(init);return json({accepted:true},202,binding);}),locks);const stamp=await api.bootstrap();const request:{file:Blob;mime:'image/png';purpose:'receipt'|'knowledge'}={file:new Blob([new Uint8Array([137,80,78,71])]),mime:'image/png',purpose:'receipt'};const prepared=api.prepareUpload(request,stamp);request.purpose='knowledge';const intent=await prepared;expect(JSON.parse(intent.material)).toMatchObject({purpose:'receipt',path:'/api/v1/files?purpose=receipt',mime:'image/png'});await api.dispatchWrite(intent.key,stamp,object);expect(bodies[0].headers).toMatchObject({'Content-Type':'image/png'});api.dispose();});
