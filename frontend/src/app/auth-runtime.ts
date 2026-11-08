import { ApiTransport } from '../api/transport';
import { ApiCoordinator, browserLocks } from './api-coordinator';
import { attachApiLifecycle } from './api-lifecycle';
import { AuthController } from './auth-controller';

export function startAuthRuntime() {
  const coordinator = new ApiCoordinator(new ApiTransport(), browserLocks());
  let channel: BroadcastChannel | undefined;
  try { channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('eai-session-v1') : undefined; } catch { /* Fresh server checks remain mandatory. */ }
  const auth = new AuthController(coordinator, () => channel?.postMessage({ type: 'context_changed' }));
  const lifecycle = attachApiLifecycle(coordinator, channel);
  const resume = () => { void auth.resume(() => document.visibilityState !== 'hidden' && navigator.onLine); };
  const receive = (event: MessageEvent<unknown>) => {
    if (event.data && typeof event.data === 'object' && Object.keys(event.data).length === 1 && 'type' in event.data && event.data.type === 'context_changed') resume();
  };
  // Lifecycle invalidation listeners were attached first; never resume stale identity on focus/BFCache.
  for (const name of ['focus', 'pageshow', 'online']) window.addEventListener(name, resume);
  document.addEventListener('visibilitychange', resume); channel?.addEventListener('message', receive);
  return { auth, api:coordinator, dispose() {
    for (const name of ['focus', 'pageshow', 'online']) window.removeEventListener(name, resume);
    document.removeEventListener('visibilitychange', resume); channel?.removeEventListener('message', receive);
    auth.dispose(); lifecycle.dispose();
  } };
}
