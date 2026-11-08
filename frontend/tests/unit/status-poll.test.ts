// @vitest-environment node
import { afterEach,expect,it,vi } from 'vitest';
import { ApiFailure } from '../../src/api/failure';
import { startStatusPoll } from '../../src/features/status-poll';
import type { PollState } from '../../src/features/status-poll';

afterEach(() => vi.useRealTimers());
it('serializes reads, backs off, stops after eight pending observations and never writes',async () => {
  vi.useFakeTimers();const states:PollState<number>[]=[];let active=0;let maximum=0;let calls=0;
  const poll=startStatusPoll(async () => {active++;maximum=Math.max(maximum,active);await new Promise((resolve) => setTimeout(resolve,50));active--;return ++calls;},() => false,(state) => states.push(state),() => true);
  await vi.advanceTimersByTimeAsync(2049);expect(calls).toBe(1);
  await vi.runAllTimersAsync();expect(calls).toBe(8);expect(maximum).toBe(1);expect(states.at(-1)).toEqual({phase:'bounded',data:8});expect(vi.getTimerCount()).toBe(0);poll.stop();
});
it('stops on terminal or any error and preserves Retry-After for explicit checks',async () => {
  vi.useFakeTimers();const complete=vi.fn(async () => 'ready');const states:PollState<string>[]=[];
  startStatusPoll(complete,() => true,(state) => states.push(state),() => true);await vi.runAllTimersAsync();expect(complete).toHaveBeenCalledTimes(1);expect(states.at(-1)?.phase).toBe('terminal');
  const read=vi.fn(async () => {throw new ApiFailure('unavailable',{status:429,retryAfterMs:30000});});
  startStatusPoll(read,() => false,(state) => states.push(state),() => true);await vi.runAllTimersAsync();expect(read).toHaveBeenCalledTimes(1);expect(states.at(-1)).toMatchObject({phase:'error',retryAt:Date.now()+30000});
});
it('clears on hidden/offline and discards an outstanding response after pause or unmount',async () => {
  const states:PollState<number>[]=[];let release!:(value:number)=>void;let signal:AbortSignal|undefined;
  const poll=startStatusPoll<number>((value) => {signal=value;return new Promise((resolve) => {release=resolve;});},() => false,(state) => states.push(state),() => true);
  poll.pause();release(123);await Promise.resolve();expect(signal?.aborted).toBe(true);expect(states.at(-1)).toEqual({phase:'paused'});
  const hidden=vi.fn(async () => 1);startStatusPoll(hidden,() => false,(state) => states.push(state),() => false);expect(hidden).not.toHaveBeenCalled();expect(states.at(-1)).toEqual({phase:'paused'});
});
