import { ApiFailure, deadline } from '../api/failure';
import { decodeIdentity, identityFingerprint, projectIdentity } from '../api/identity';
import type { IdentityView } from '../api/identity';
import type { Identity } from '../api/identity';
import { IntentStore } from '../api/intents';
import type { Intent } from '../api/intents';
import { describeWrite, decodeWrite, writeKeyField } from '../api/write-policy';
import type { JsonWrite } from '../api/write-policy';
import { describeUpload, decodeUpload, maximumUploadBytes } from '../api/upload-policy';
import type { UploadMime } from '../api/upload-policy';
import { ApiTransport, isUuid } from '../api/transport';
import type { Decoder } from '../api/transport';

export const sessionLockName = 'eai-session-context-v1';
export interface LockPort { request<T>(name: string, options: { mode: 'exclusive'; signal: AbortSignal }, callback: () => Promise<T>): Promise<T> }
export interface ContextStamp { readonly generation: number; readonly fingerprint: string; readonly binding: string }
export type SessionChange = { kind: 'logout' } | { kind: 'switch'; membershipId: string };

export class ApiCoordinator {
  #transport: ApiTransport; #locks?: LockPort; #timeout: number;
  #generation = 0; #identity: Identity | null = null; #boot?: Promise<ContextStamp>;
  #active = new Set<AbortController>(); #listeners = new Set<() => void>();
  #sessionPending = false;
  #uploads = new Map<string,Blob>(); #uploadHash?: symbol;
  readonly intents = new IntentStore();
  constructor(transport: ApiTransport, locks?: LockPort, timeoutMs = 10_000) {
    this.#transport = transport; this.#locks = locks; this.#timeout = timeoutMs;
  }
  get generation(): number { return this.#generation; }
  identityView(expected: ContextStamp): IdentityView {
    if (!this.#matches(expected)) throw new ApiFailure('context_changed');
    return projectIdentity(this.#identity!);
  }
  subscribeInvalidation(listener: () => void): () => void { this.#listeners.add(listener); return () => { this.#listeners.delete(listener); }; }
  invalidate(): void {
    this.#generation++; this.#identity = null; this.#boot = undefined; this.intents.clear(); this.#uploads.clear();
    for (const active of this.#active) active.abort();
    for (const listener of this.#listeners) { try { listener(); } catch { /* One view cannot prevent other views clearing. */ } }
  }
  #stamp(identity: Identity): ContextStamp { return Object.freeze({ generation: this.#generation, fingerprint: identityFingerprint(identity), binding: identity.context_binding }); }
  #matches(stamp: ContextStamp): boolean { return !!this.#identity && stamp.generation === this.#generation && stamp.fingerprint === identityFingerprint(this.#identity); }
  async #run<T>(work: (signal: AbortSignal) => Promise<T>, caller?: AbortSignal): Promise<T> {
    if (!this.#locks) throw new ApiFailure('unsupported');
    const controller = new AbortController();
    const cancel = () => controller.abort();
    caller?.addEventListener('abort', cancel, { once: true });
    if (caller?.aborted) cancel();
    this.#active.add(controller);
    try {
      return await deadline((signal) => this.#locks!.request(sessionLockName, { mode: 'exclusive', signal }, () => {
        if (signal.aborted) throw new ApiFailure('cancelled');
        return work(signal);
      }), this.#timeout, controller.signal);
    } finally { this.#active.delete(controller); caller?.removeEventListener('abort', cancel); }
  }
  #fresh(signal: AbortSignal): Promise<Identity> {
    return this.#transport.request({ path: '/api/v1/me', bootstrap: true, decode: decodeIdentity }, signal, this.#timeout);
  }
  bootstrap(): Promise<ContextStamp> {
    if (this.#boot) return this.#boot;
    const generation = this.#generation;
    const work = this.#run(async (signal) => {
      const identity = await this.#fresh(signal);
      if (signal.aborted || generation !== this.#generation) throw new ApiFailure('context_changed');
      if (this.#identity && identityFingerprint(this.#identity) !== identityFingerprint(identity)) throw new ApiFailure('context_changed');
      this.#identity = identity; return this.#stamp(identity);
    }).catch((error: unknown) => { if (generation === this.#generation) this.invalidate(); throw error instanceof ApiFailure ? error : new ApiFailure('unavailable'); })
      .finally(() => { if (this.#boot === work) this.#boot = undefined; });
    this.#boot = work; return work;
  }
  privateRead<T>(request: { path: string; decode: Decoder<T> }, expected: ContextStamp, caller?: AbortSignal): Promise<T> {
    return this.#privateRead(request,expected,caller,false);
  }
  resourceRead<T>(request: { path: string; decode: Decoder<T> }, expected: ContextStamp, caller?: AbortSignal): Promise<T> {
    return this.#privateRead(request,expected,caller,true);
  }
  receiptRead(fileId:string,claimId:string,expected:ContextStamp,signal?:AbortSignal):Promise<Blob>{if(!isUuid(fileId)||!isUuid(claimId))throw new ApiFailure('input');return this.#privateRead({path:'/api/v1/files/'+fileId+'/claims/'+claimId+'/content',download:true,decode:(v)=>{if(!(v instanceof Blob))throw new ApiFailure('malformed');return v;}},expected,signal,true);}
  async #privateRead<T>(request: { path: string; decode: Decoder<T>; download?:boolean }, expected: ContextStamp, caller: AbortSignal | undefined, preserveVerifiedRejection: boolean): Promise<T> {
    const descriptor = { path: request.path, decode: request.decode, download:request.download };
    const stamp = { ...expected };
    if (!this.#matches(stamp) || this.#identity?.context_binding !== stamp.binding) throw new ApiFailure('context_changed');
    if (!this.#identity.active_tenant_id) throw new ApiFailure('forbidden');
    // Only rolled-out JSON private reads; binary downloads need their own bounded consumer.
    if (!descriptor.download && (!/^\/api\/v1\/(?:files\/[^/?]+\/status|(?:trips|drafts|actions|claims|approvals|admin|operator|tenant)(?:\/|\?|$)|knowledge\/|organization\/|conversations(?:\/|\?|$)|runs\/)/.test(descriptor.path) ||
        descriptor.path.startsWith('/api/v1/files/') && descriptor.path.endsWith('/content'))) throw new ApiFailure('input');
    let verifiedFailure = false;
    try {
      return await this.#run(async (signal) => {
        if (!this.#matches(stamp)) throw new ApiFailure('context_changed');
        const before = await this.#fresh(signal);
        if (identityFingerprint(before) !== stamp.fingerprint || !this.#matches(stamp)) throw new ApiFailure('context_changed');
        let buffer: unknown; let failure: ApiFailure | undefined;
        try { buffer = await this.#transport.request({ path: descriptor.path, decode: (value) => value, download:descriptor.download, expectedContext: stamp.binding }, signal, this.#timeout); }
        catch (error) { if (!preserveVerifiedRejection) throw error; failure = error instanceof ApiFailure ? error : new ApiFailure('unavailable'); }
        const after = await this.#fresh(signal);
        if (signal.aborted || !this.#matches(stamp) || identityFingerprint(after) !== stamp.fingerprint) throw new ApiFailure('context_changed');
        this.#identity = after;
        if (failure) {
          verifiedFailure = preserveVerifiedRejection && !['unauthenticated','context_changed','binding_required','malformed'].includes(failure.kind);
          throw failure;
        }
        return descriptor.decode(buffer);
      }, caller);
    } catch (error) {
      // A view may cancel an obsolete read while another view still owns the
      // same identity. Its buffer is discarded; auth invalidation has its own
      // generation and must not be triggered by routine effect cleanup.
      const viewCancelled = preserveVerifiedRejection && caller?.aborted && error instanceof ApiFailure && ['cancelled','timeout'].includes(error.kind);
      if (!verifiedFailure && !viewCancelled && stamp.generation === this.#generation) this.invalidate();
      throw error instanceof ApiFailure ? error : new ApiFailure('unavailable');
    }
  }

  prepareWrite(request: JsonWrite, expected: ContextStamp, key?: string): Intent {
    if (!this.#matches(expected) || expected.binding !== this.#identity?.context_binding) throw new ApiFailure('context_changed');
    if (!this.#identity.active_tenant_id) throw new ApiFailure('forbidden');
    const descriptor = describeWrite(request);
    return this.intents.prepare(`${descriptor.method} ${descriptor.path}`,expected.generation,descriptor,key);
  }

  async prepareUpload(request:{file:Blob;mime:UploadMime;collectionId?:string|null;purpose?:'knowledge'|'receipt'},expected:ContextStamp,key?:string,caller?:AbortSignal):Promise<Intent> {
    const stamp={...expected};
    const {file:source,mime}=request;const purpose=request.purpose ?? 'knowledge';const collection=request.collectionId ?? null;
    if (!this.#matches(stamp) || stamp.binding!==this.#identity?.context_binding) throw new ApiFailure('context_changed');
    if (!this.#identity.active_tenant_id) throw new ApiFailure('forbidden');
    if (this.#uploadHash || this.#uploads.size) throw new ApiFailure('conflict');
    if (!(source instanceof Blob) || source.size<1 || source.size>maximumUploadBytes || !(purpose==='receipt'?['application/pdf','image/jpeg','image/png']:['application/pdf','text/plain','text/markdown']).includes(mime)) throw new ApiFailure('input');
    if (!globalThis.crypto?.subtle) throw new ApiFailure('unsupported');
    if (caller?.aborted) throw new ApiFailure('cancelled');
    const marker=Symbol();this.#uploadHash=marker;
    try {
      const file=source.slice(0,source.size,mime);
      const hashed=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
      if (caller?.aborted) throw new ApiFailure('cancelled');
      if (!this.#matches(stamp)) throw new ApiFailure('context_changed');
      const digest=Array.from(new Uint8Array(hashed),(value) => value.toString(16).padStart(2,'0')).join('');
      const descriptor=describeUpload(mime,collection,file.size,digest,purpose);
      const intent=this.intents.prepare(`POST ${descriptor.path}`,stamp.generation,descriptor,key);
      this.#uploads.set(intent.key,file);return intent;
    } finally { if (this.#uploadHash===marker) this.#uploadHash=undefined; }
  }

  discardPrepared(key:string):void {
    if (this.intents.current(key)?.state!=='prepared') throw new ApiFailure('conflict');
    this.intents.discard(key);this.#uploads.delete(key);
  }

  // There is one upload across all collection paths. A reopened view must find
  // the original unresolved intent, including a dispatch still in flight.
  uploadIntent(expected:ContextStamp):Intent|undefined {
    if (!this.#matches(expected) || expected.binding!==this.#identity?.context_binding) return undefined;
    for (const key of this.#uploads.keys()) {
      const intent=this.intents.current(key);
      if (intent?.generation===expected.generation) return intent;
    }
    return undefined;
  }

  async dispatchWrite<T>(key: string, expected: ContextStamp, decode: Decoder<T>, options: { reconcile?: boolean; signal?: AbortSignal } = {}): Promise<T> {
    const stamp = { ...expected }; const initial = this.intents.current(key);
    if (!this.#matches(stamp) || stamp.binding !== this.#identity?.context_binding || initial?.generation !== stamp.generation) throw new ApiFailure('context_changed');
    if (initial.state !== (options.reconcile ? 'unknown' : 'prepared')) throw new ApiFailure('conflict');
    const kind=JSON.parse(initial.material) as {kind?:unknown};
    const descriptor = kind.kind==='upload' ? decodeUpload(initial.material) : decodeWrite(initial.material);
    if (initial.resource !== `${descriptor.method} ${descriptor.path}`) throw new ApiFailure('input');
    const upload=descriptor.kind==='upload' ? this.#uploads.get(key) : undefined;
    if (descriptor.kind==='upload' && (!upload || upload.size!==descriptor.size)) throw new ApiFailure('input');
    let active: Intent | undefined; let dispatched = false; let verified = false; let closed = false;
    const verify = async (signal: AbortSignal) => {
      const fresh = await this.#fresh(signal);
      if (signal.aborted || !this.#matches(stamp) || identityFingerprint(fresh) !== stamp.fingerprint || fresh.context_binding !== stamp.binding) throw new ApiFailure('context_changed');
      this.#identity = fresh; return fresh;
    };
    try {
      const result = await this.#run(async (signal) => {
        if (closed || !this.#matches(stamp)) throw new ApiFailure('context_changed');
        if (this.intents.current(key) !== initial) throw new ApiFailure('conflict');
        const fresh = await verify(signal);
        if (closed || signal.aborted) throw new ApiFailure('cancelled');
        active = this.intents.transition(initial,'dispatching',stamp.generation);
        let buffer: unknown; let failure: ApiFailure | undefined;
        dispatched = true;
        try {
          buffer = await this.#transport.request({ path: descriptor.path, method: descriptor.method,
            body: descriptor.kind==='upload' ? upload! : JSON.stringify({ ...descriptor.body, ...(writeKeyField(descriptor.method,descriptor.path)?{[writeKeyField(descriptor.method,descriptor.path)!]:key}:{}) }),
            ...(descriptor.kind==='upload' ? {mime:descriptor.mime,idempotencyKey:key} : {}),
            csrf: fresh.csrf_token, expectedContext: stamp.binding, expectedStatus: descriptor.status, decode: (value) => value },signal,this.#timeout);
        } catch (error) { failure = error instanceof ApiFailure ? error : new ApiFailure('unavailable',{ outcome: 'unknown' }); }
        if (closed || signal.aborted) throw new ApiFailure('cancelled',{ outcome: 'unknown' });
        await verify(signal);
        if (closed || signal.aborted) throw new ApiFailure('cancelled',{ outcome: 'unknown' });
        verified = true;
        if (failure) throw failure;
        try { return decode(buffer); }
        catch { throw new ApiFailure('malformed',{ outcome: 'unknown' }); }
      },options.signal);
      closed = true;
      if (!this.#matches(stamp) || !active || this.intents.current(key) !== active) throw new ApiFailure('context_changed',{ outcome: 'unknown' });
      this.intents.transition(active,'settled',stamp.generation); this.intents.discard(key); this.#uploads.delete(key);
      return result;
    } catch (error) {
      closed = true;
      const safe = error instanceof ApiFailure ? error : new ApiFailure('unavailable');
      const fatal = ['context_changed','binding_required','unauthenticated'].includes(safe.kind);
      // A timed-out/lost reply may still have committed. One separate bounded
      // identity read can retain the exact intent for explicit reconciliation.
      // This never repeats the business request or follows a cancelled caller.
      if (dispatched && !verified && !fatal && !options.signal?.aborted && this.#matches(stamp)) {
        try { await this.#run(async (signal) => { await verify(signal); },undefined); verified = true; }
        catch { /* Context is closed below if this bounded verification fails. */ }
      }
      const unknown = dispatched && safe.outcome !== 'rejected';
      if (active && this.intents.current(key) === active) {
        this.intents.transition(active,unknown ? 'unknown' : 'settled',stamp.generation);
        if (!unknown) { this.intents.discard(key); this.#uploads.delete(key); }
      }
      if ((!verified && dispatched || fatal) && stamp.generation === this.#generation) this.invalidate();
      if (unknown) throw new ApiFailure(safe.kind,{ status: safe.status, requestId: safe.requestId, retryAfterMs: safe.retryAfterMs, outcome: 'unknown' });
      throw safe;
    } finally { closed = true; }
  }

  async changeSession(change: SessionChange, expected: ContextStamp, caller?: AbortSignal): Promise<void> {
    if (!this.#matches(expected)) throw new ApiFailure('context_changed');
    if (change.kind === 'switch' && !isUuid(change.membershipId)) throw new ApiFailure('input');
    if (this.#sessionPending) throw new ApiFailure('conflict');
    const action = { ...change };
    const stamp = { ...expected };
    this.#sessionPending = true;
    let dispatched = false;
    try {
      await this.#run(async (signal) => {
        if (!this.#matches(stamp)) throw new ApiFailure('context_changed');
        const fresh = await this.#fresh(signal);
        if (!this.#matches(stamp) || identityFingerprint(fresh) !== stamp.fingerprint) throw new ApiFailure('context_changed');
        if (action.kind === 'switch' && !fresh.memberships.some((item) => item.membership_id === action.membershipId)) throw new ApiFailure('forbidden');
        dispatched = true;
        if (action.kind === 'logout') {
          await this.#transport.request({ path: '/api/v1/auth/logout', method: 'POST', body: '{}', csrf: fresh.csrf_token,
            empty: true, decode: (value) => { if (value !== null) throw new ApiFailure('malformed'); } }, signal, this.#timeout);
        } else {
          const switched = await this.#transport.request({ path: '/api/v1/session/tenant', method: 'POST',
            body: JSON.stringify({ membership_id: action.membershipId }), csrf: fresh.csrf_token, decode: decodeIdentity }, signal, this.#timeout);
          if (switched.principal_id !== fresh.principal_id || switched.active_membership_id !== action.membershipId ||
              switched.context_binding === fresh.context_binding ||
              switched.active_tenant_id !== fresh.memberships.find((item) => item.membership_id === action.membershipId)?.tenant_id) throw new ApiFailure('malformed');
        }
      }, caller);
    } catch (error) {
      const safe = error instanceof ApiFailure ? error : new ApiFailure('unavailable');
      if (dispatched && (safe.outcome === 'not_dispatched' || safe.outcome === 'unknown')) {
        throw new ApiFailure(safe.kind, { status: safe.status, requestId: safe.requestId, outcome: 'unknown' });
      }
      throw safe;
    } finally { this.#sessionPending = false; this.invalidate(); } // Always mask/reconcile; never replay a session write automatically.
  }
  dispose(): void { this.invalidate(); this.#listeners.clear(); }
}

export function browserLocks(): LockPort | undefined {
  return globalThis.isSecureContext && typeof navigator !== 'undefined' && navigator.locks ? navigator.locks : undefined;
}
