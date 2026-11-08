import { ApiFailure } from './failure';
import { isUuid } from './transport';

export interface Intent {
  readonly key: string; readonly generation: number; readonly resource: string; readonly material: string;
  readonly state: 'prepared' | 'dispatching' | 'unknown' | 'settled';
}
// Memory only. Keys belong to an intent, not a fresh click/retry; no dispatch/replay here.
export class IntentStore {
  #entries = new Map<string, Intent>();
  #revision=0;#listeners=new Set<()=>void>();
  getSnapshot=():number => this.#revision;
  subscribe=(listener:()=>void):(()=>void) => {this.#listeners.add(listener);return () => {this.#listeners.delete(listener);};};
  #emit():void {this.#revision++;for (const listener of this.#listeners) {try {listener();} catch { /* One consumer cannot block intent settlement. */ }}}
  current(key: string): Intent | undefined { return this.#entries.get(key); }
  pending(resource: string, generation: number): Intent | undefined {
    return [...this.#entries.values()].find((entry) => entry.resource === resource && entry.generation === generation && ['dispatching','unknown'].includes(entry.state));
  }
  pendingEntries(generation:number):readonly Intent[]{return [...this.#entries.values()].filter(i=>i.generation===generation&&['prepared','dispatching','unknown'].includes(i.state));}
  discard(key: string): void {
    const entry = this.#entries.get(key);
    if (entry && ['unknown','dispatching'].includes(entry.state)) throw new ApiFailure('conflict');
    if (this.#entries.delete(key)) this.#emit();
  }
  prepare(resource: string, generation: number, value: unknown, key: string = crypto.randomUUID()): Intent {
    if (!resource || !Number.isSafeInteger(generation) || generation < 0 || !isUuid(key) || this.#entries.has(key)) throw new ApiFailure('input');
    if ([...this.#entries.values()].some((entry) => entry.resource === resource && entry.generation === generation && ['unknown','dispatching'].includes(entry.state))) throw new ApiFailure('conflict');
    let material: string | undefined;
    try { material = JSON.stringify(value); } catch { throw new ApiFailure('input'); }
    if (!material || new TextEncoder().encode(material).length > 1_048_576) throw new ApiFailure('input');
    const intent: Intent = Object.freeze({ key, generation, resource, material, state: 'prepared' });
    this.#entries.set(key, intent);this.#emit();
    return intent;
  }
  transition(intent: Intent, state: Intent['state'], generation: number): Intent {
    if (generation !== intent.generation || this.#entries.get(intent.key) !== intent) throw new ApiFailure('context_changed');
    const allowed: Record<Intent['state'], readonly Intent['state'][]> = { prepared: ['dispatching'], dispatching: ['unknown','settled'], unknown: ['dispatching','settled'], settled: [] };
    if (!allowed[intent.state].includes(state)) throw new ApiFailure('conflict');
    if (state === 'dispatching' && [...this.#entries.values()].some((entry) => entry.key !== intent.key &&
      entry.resource === intent.resource && entry.generation === generation && ['unknown','dispatching'].includes(entry.state))) throw new ApiFailure('conflict');
    const next = Object.freeze({ ...intent, state }); this.#entries.set(intent.key, next);this.#emit();return next;
  }
  clear(): void { if (this.#entries.size) {this.#entries.clear();this.#emit();} }
}
