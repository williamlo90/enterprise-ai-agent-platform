export type FailureKind = 'input' | 'unauthenticated' | 'forbidden' | 'not_found' | 'conflict' |
  'unavailable' | 'malformed' | 'network' | 'timeout' | 'cancelled' | 'unsupported' | 'context_changed' | 'binding_required';

// Fixed public message: never attach raw response, URL, exception cause or request material.
export class ApiFailure extends Error {
  readonly kind: FailureKind;
  readonly status?: number;
  readonly requestId?: string;
  readonly retryAfterMs?: number;
  readonly outcome: 'not_dispatched' | 'rejected' | 'unknown';
  constructor(kind: FailureKind, details: { status?: number; requestId?: string; retryAfterMs?: number;
    outcome?: 'not_dispatched' | 'rejected' | 'unknown' } = {}) {
    super('The operation could not be completed.');
    this.name = 'ApiFailure';
    this.kind = kind;
    this.status = details.status;
    this.requestId = details.requestId;
    this.retryAfterMs = details.retryAfterMs;
    this.outcome = details.outcome ?? 'not_dispatched';
  }
}

export async function deadline<T>(work: (signal: AbortSignal) => Promise<T>, milliseconds: number, parent?: AbortSignal): Promise<T> {
  if (!Number.isInteger(milliseconds) || milliseconds < 1 || milliseconds > 30_000) throw new ApiFailure('input');
  const controller = new AbortController();
  let timedOut = false;
  const abort = () => controller.abort();
  const timer = setTimeout(() => { timedOut = true; abort(); }, milliseconds);
  parent?.addEventListener('abort', abort, { once: true });
  if (parent?.aborted) abort();
  let rejectAbort: () => void = () => {};
  const interrupted = new Promise<never>((_, reject) => {
    rejectAbort = () => reject(new ApiFailure(timedOut ? 'timeout' : 'cancelled'));
    controller.signal.addEventListener('abort', rejectAbort, { once: true });
  });
  try {
    if (controller.signal.aborted) throw new ApiFailure('cancelled');
    return await Promise.race([work(controller.signal), interrupted]);
  } catch (error) {
    // Native Web Locks may reject AbortError before the race's own abort promise.
    if (controller.signal.aborted) throw new ApiFailure(timedOut ? 'timeout' : 'cancelled');
    throw error;
  } finally {
    clearTimeout(timer);
    parent?.removeEventListener('abort', abort);
    controller.signal.removeEventListener('abort', rejectAbort);
  }
}
