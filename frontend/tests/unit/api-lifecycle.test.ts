import { expect, it, vi } from 'vitest';
import { attachApiLifecycle } from '../../src/app/api-lifecycle';
import type { ChannelPort } from '../../src/app/api-lifecycle';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';

it('masks context on lifecycle events, clears transient intents, and removes its listeners on disposal', () => {
  const api = new ApiCoordinator(new ApiTransport());
  const clear = vi.fn(); api.subscribeInvalidation(clear);
  const lifecycle = attachApiLifecycle(api);
  for (const name of ['focus','pageshow','offline','online']) window.dispatchEvent(new Event(name));
  expect(clear).toHaveBeenCalledTimes(4);
  lifecycle.dispose();
  window.dispatchEvent(new Event('focus'));
  expect(clear).toHaveBeenCalledTimes(5); // final explicit dispose invalidation only
});

it('broadcasts only fixed metadata and ignores payload-bearing/unrecognized messages', () => {
  const api = new ApiCoordinator(new ApiTransport()); let receive: ((event: MessageEvent<unknown>) => void) | undefined;
  const channel: ChannelPort = { postMessage: vi.fn(), addEventListener: (_name, callback) => { receive = callback; }, removeEventListener: vi.fn(), close: vi.fn() };
  const lifecycle = attachApiLifecycle(api, channel);
  lifecycle.signal('csrf_rotated');
  expect(channel.postMessage).toHaveBeenCalledWith({ type: 'csrf_rotated' });
  receive?.(new MessageEvent('message', { data: { type: 'context_changed', tenant: 'not accepted' } }));
  expect(api.generation).toBe(0);
  receive?.(new MessageEvent('message', { data: { type: 'context_changed' } }));
  expect(api.generation).toBe(1);
  expect(() => lifecycle.signal('private payload' as never)).toThrow();
  lifecycle.dispose();
  expect(channel.close).toHaveBeenCalledTimes(1);
});
