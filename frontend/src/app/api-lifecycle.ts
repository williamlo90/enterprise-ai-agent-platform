import type { ApiCoordinator } from './api-coordinator';
import { ApiFailure } from '../api/failure';

export type InvalidationSignal = 'context_changed' | 'csrf_rotated';
export interface ChannelPort {
  postMessage: (value: { type: InvalidationSignal }) => void;
  addEventListener: (type: 'message', callback: (event: MessageEvent<unknown>) => void) => void;
  removeEventListener: (type: 'message', callback: (event: MessageEvent<unknown>) => void) => void;
  close: () => void;
}

// Explicit owner setup; importing the module creates no listener, channel, server or API call.
export function attachApiLifecycle(coordinator: ApiCoordinator, channel?: ChannelPort,
  page: Window = window, documentPort: Document = document): { signal: (type: InvalidationSignal) => void; dispose: () => void } {
  const invalidate = () => coordinator.invalidate();
  const visibility = () => { if (documentPort.visibilityState === 'hidden') invalidate(); };
  const receive = (event: MessageEvent<unknown>) => {
    const data = event.data;
    if (!data || typeof data !== 'object' || Array.isArray(data) || Object.keys(data).length !== 1 || !('type' in data)) return;
    if (data.type === 'context_changed') invalidate();
    // csrf_rotated carries no authority; every write already bootstraps a fresh token.
  };
  for (const name of ['offline','online','focus','pageshow']) page.addEventListener(name, invalidate);
  documentPort.addEventListener('visibilitychange', visibility);
  channel?.addEventListener('message', receive);
  return {
    signal(type) {
      if (type !== 'context_changed' && type !== 'csrf_rotated') throw new ApiFailure('input');
      if (type === 'context_changed') invalidate();
      channel?.postMessage({ type });
    },
    dispose() {
      for (const name of ['offline','online','focus','pageshow']) page.removeEventListener(name, invalidate);
      documentPort.removeEventListener('visibilitychange', visibility);
      channel?.removeEventListener('message', receive); channel?.close(); coordinator.dispose();
    },
  };
}
