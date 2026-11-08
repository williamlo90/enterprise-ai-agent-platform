import { ApiFailure, deadline } from './failure';

export type Decoder<T> = (value: unknown) => T;
export type FetchPort = (path: string, init: RequestInit) => Promise<Response>;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function isUuid(value: unknown): value is string { return typeof value === 'string' && uuid.test(value); }
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ApiFailure('malformed');
  return value as Record<string, unknown>;
}

export function retryAfter(value: string | null, now = Date.now()): number | undefined {
  if (!value || value.length > 80) return undefined;
  const seconds = /^\d+$/.test(value) ? Number(value) * 1000 : Date.parse(value) - now;
  return Number.isFinite(seconds) ? Math.max(0, Math.min(300_000, seconds)) : undefined;
}

async function jsonBody(response: Response, signal: AbortSignal): Promise<unknown> {
  const maxBytes = 1_048_576; // Initial client resource ceiling, not a document/payload business limit.
  const reject = () => { void response.body?.cancel().catch(() => {}); throw new ApiFailure('malformed'); };
  if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') ?? '')) reject();
  const declared = response.headers.get('content-length');
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > maxBytes)) reject();
  const reader = response.body?.getReader();
  if (!reader) throw new ApiFailure('malformed');
  const chunks: Uint8Array[] = [];
  let bytes = 0, complete = false;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    while (true) {
      if (signal.aborted) throw new ApiFailure('cancelled');
      const chunk = await reader.read();
      if (chunk.done) { complete = true; break; }
      bytes += chunk.value.length;
      if (bytes > maxBytes) throw new ApiFailure('malformed');
      chunks.push(chunk.value);
    }
    const joined = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.length; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(joined)) as unknown;
  } catch { throw new ApiFailure(signal.aborted ? 'cancelled' : 'malformed'); }
  finally {
    signal.removeEventListener('abort', cancel);
    if (!complete) cancel();
    reader.releaseLock();
  }
}

async function receiptBody(response:Response,signal:AbortSignal):Promise<Blob> {
  const max=10*1024*1024;const reader=response.body?.getReader();if(!reader)throw new ApiFailure('malformed');
  const cancel=()=>{void reader.cancel().catch(()=>{});};signal.addEventListener('abort',cancel,{once:true});let done=false;
  try{if(response.headers.get('content-type')?.split(';')[0]!=='application/octet-stream'||!response.headers.get('content-disposition')?.startsWith('attachment;'))throw new ApiFailure('malformed');const declared=response.headers.get('content-length');if(declared&&(!/^[0-9]+$/.test(declared)||Number(declared)>max))throw new ApiFailure('malformed');const chunks:Uint8Array[]=[];let total=0;while(true){if(signal.aborted)throw new ApiFailure('cancelled');const part=await reader.read();if(part.done){done=true;break;}total+=part.value.length;if(total>max)throw new ApiFailure('malformed');chunks.push(part.value);}if(!total)throw new ApiFailure('malformed');const joined=new Uint8Array(total);let offset=0;for(const chunk of chunks){joined.set(chunk,offset);offset+=chunk.length;}return new Blob([joined],{type:'application/octet-stream'});}
  finally{signal.removeEventListener('abort',cancel);if(!done)cancel();reader.releaseLock();}
}

export interface RequestSpec<T> {
  path: string; method?: 'GET' | 'POST' | 'PATCH'; body?: string | Blob; csrf?: string;
  mime?: 'application/pdf' | 'text/plain' | 'text/markdown' | 'image/jpeg' | 'image/png';
  download?: boolean; bootstrap?: boolean; idempotencyKey?: string; decode: Decoder<T>; empty?: boolean; expectedStatus?: 200 | 201 | 202;
  expectedContext?: string;
}

// Internal transport primitive. Feature consumers go through the context coordinator.
// No arbitrary RequestInit, origins, redirect policy, credential or header overrides.
export class ApiTransport {
  #fetch: FetchPort;
  constructor(fetchPort: FetchPort = (path, init) => fetch(path, init)) { this.#fetch = fetchPort; }

  async request<T>(spec: RequestSpec<T>, signal?: AbortSignal, timeoutMs = 10_000): Promise<T> {
    spec = { ...spec };
    if (!/^\/api\/v1\/[a-z0-9_/-]+(?:\?[a-zA-Z0-9_%=&+.*-]*)?$/.test(spec.path) ||
        spec.path.includes('//') || /\/auth\/(?:login|callback)/.test(spec.path)) throw new ApiFailure('input');
    if (spec.method !== undefined && !['GET','POST','PATCH'].includes(spec.method)) throw new ApiFailure('input');
    if(spec.download && (spec.method!==undefined && spec.method!=='GET' || !/^\/api\/v1\/files\/[a-f0-9-]{36}\/claims\/[a-f0-9-]{36}\/content$/.test(spec.path) || !spec.expectedContext))throw new ApiFailure('input');
    const write = spec.method !== undefined && spec.method !== 'GET';
    const binary = spec.body instanceof Blob;
    if (binary && (spec.method!=='POST' || !/^\/api\/v1\/files\?purpose=(?:receipt|knowledge(?:&collection_id=[a-f0-9-]{36})?)$/.test(spec.path) || !(spec.path==='/api/v1/files?purpose=receipt'?['application/pdf','image/jpeg','image/png']:['application/pdf','text/plain','text/markdown']).includes(spec.mime ?? '') ||
        (spec.body as Blob).size<1 || (spec.body as Blob).size>10*1024*1024 || !spec.idempotencyKey || !spec.expectedContext || spec.expectedStatus!==202)) throw new ApiFailure('input');
    if (!binary && spec.mime!==undefined) throw new ApiFailure('input');
    if (write && (!spec.csrf || !/^[A-Za-z0-9_-]{43}$/.test(spec.csrf))) throw new ApiFailure('input');
    if (!write && (spec.body !== undefined || spec.csrf !== undefined)) throw new ApiFailure('input');
    if (spec.idempotencyKey && !isUuid(spec.idempotencyKey)) throw new ApiFailure('input');
    if (spec.expectedContext !== undefined && !/^[a-f0-9]{64}$/.test(spec.expectedContext)) throw new ApiFailure('input');
    let dispatched = false;
    try {
      return await deadline(async (bounded) => {
        const headers: Record<string, string> = { Accept: 'application/json' };
        if (spec.bootstrap) headers['X-EAI-CSRF-Bootstrap'] = '1';
        if (write) { headers['Content-Type'] = binary ? spec.mime! : 'application/json'; headers['X-CSRF-Token'] = spec.csrf!; }
        if (spec.idempotencyKey) headers['Idempotency-Key'] = spec.idempotencyKey;
        if (spec.expectedContext) headers['X-EAI-Expected-Context'] = spec.expectedContext;
        dispatched = true;
        const response = await this.#fetch(spec.path, { method: spec.method ?? 'GET', body: spec.body,
          headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error', referrerPolicy: 'no-referrer', signal: bounded });
        if (bounded.aborted) { void response.body?.cancel().catch(() => {}); throw new ApiFailure('cancelled'); }
        if (!response.ok) {
          let requestId: string | undefined;
          try { const envelope = object(await jsonBody(response, bounded)); const error = object(envelope.error);
            if (typeof error.code === 'string' && typeof error.message === 'string' && typeof error.retryable === 'boolean' && isUuid(error.request_id)) requestId = error.request_id;
          } catch { /* HTTP status remains useful; never surface an untrusted body. */ }
          const status = response.status;
          const kind = status === 401 ? 'unauthenticated' : status === 403 ? 'forbidden' : status === 404 ? 'not_found' : status === 409 ? 'conflict' : [400,413,415,422].includes(status) ? 'input' : 'unavailable';
          throw new ApiFailure(kind, { status, requestId, retryAfterMs: retryAfter(response.headers.get('retry-after')),
            outcome: write && (status >= 500 || status === 408 || status === 401 || binary && ![400,413,415,422].includes(status)) ? 'unknown' : 'rejected' });
        }
        if (spec.expectedContext) {
          const actual = response.headers.get('X-EAI-Context-Binding');
          if (!actual || actual !== spec.expectedContext) {
            void response.body?.cancel().catch(() => {});
            throw new ApiFailure(actual ? 'context_changed' : 'binding_required');
          }
        }
        if (spec.empty) {
          if (response.status !== 204) throw new ApiFailure('malformed');
          return spec.decode(null);
        }
        if (response.status !== (spec.expectedStatus ?? 200)) throw new ApiFailure('malformed');
        return spec.decode(spec.download?await receiptBody(response,bounded):await jsonBody(response, bounded));
      }, timeoutMs, signal);
    } catch (error) {
      const safe = error instanceof ApiFailure ? error : new ApiFailure('network');
      if (write && dispatched && safe.outcome === 'not_dispatched') {
        throw new ApiFailure(safe.kind, { status: safe.status, requestId: safe.requestId, retryAfterMs: safe.retryAfterMs, outcome: 'unknown' });
      }
      throw safe;
    }
  }
}
