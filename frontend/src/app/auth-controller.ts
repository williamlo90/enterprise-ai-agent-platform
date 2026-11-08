import { ApiFailure } from '../api/failure';
import type { FailureKind } from '../api/failure';
import type { IdentityView } from '../api/identity';
import type { ApiCoordinator, ContextStamp, SessionChange } from './api-coordinator';

export type AuthState = Readonly<{
  kind: 'booting' | 'signed_out' | 'unavailable' | 'invalid' | 'no_membership' | 'needs_tenant' | 'ready' | 'pending';
  identity?: IdentityView; stamp?: ContextStamp; error?: FailureKind; unknown?: boolean;
}>;
export function landing(identity: IdentityView): '/chat' | '/knowledge' | '/organization' | '/access' | '/approvals' | '/admin' | '/operator' {
  const roles = identity.memberships.find((m) => m.membership_id === identity.active_membership_id)?.roles ?? [];
  if (roles.some((r) => r.role === 'employee')) return '/chat';
  if (roles.some((r) => r.role === 'knowledge_manager')) return '/knowledge';
  if (roles.some((r) => r.role === 'tenant_admin')) return '/organization';
  if(roles.some(r=>r.role==='approver'&&r.scope_kind==='process'&&r.scope_ref==='reimbursement'))return '/approvals';
  if(roles.some(r=>r.role==='process_owner'&&r.scope_ref==='reimbursement'))return '/admin';
  if(roles.some(r=>r.role==='operator_scoped'&&r.scope_kind==='operations'))return '/operator';
  return '/access';
}
export function canOpen(identity: IdentityView, path: string): boolean {
  if (path === '/access' || path === '/tenants') return true;
  const roles=identity.memberships.find(m=>m.membership_id===identity.active_membership_id)?.roles??[];
  const admin=roles.some(r=>r.role==='tenant_admin'&&r.scope_kind==='tenant'&&r.scope_ref===identity.active_tenant_id);
  const approver=roles.some(r=>r.role==='approver'&&r.scope_kind==='process'&&r.scope_ref==='reimbursement');
  const owner=roles.some(r=>r.role==='process_owner'&&r.scope_kind==='process'&&r.scope_ref==='reimbursement');
  const operator=(scope:string)=>roles.some(r=>r.role==='operator_scoped'&&r.scope_kind==='operations'&&r.scope_ref===scope);
  if(/^\/approvals(?:\/[a-f0-9-]{36})?$/.test(path))return approver;
  if(path==='/tasks')return approver||roles.some(r=>r.role==='employee');
  if(path==='/admin')return admin||owner;
  if(path==='/admin/policies/new')return owner;
  if(/^\/admin\/policies(?:\/[a-f0-9-]{36}\/[1-9][0-9]*)?$/.test(path))return admin||owner;
  if(/^\/admin\/(?:memberships(?:\/[a-f0-9-]{36})?|config|audit|jobs)$/.test(path))return admin;
  if(path==='/operator')return operator('provisioning')||operator('job_metadata');
  if(path==='/operator/tenants')return operator('provisioning');
  if(path==='/operator/jobs')return operator('job_metadata');
  if (path==='/search' || /^\/sources\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(path)) return !!identity.active_tenant_id;
  const role = /^\/reimbursements(?:\/(?:drafts|claims|actions)\/[0-9a-f-]{36})?$/.test(path) ? 'employee' : path === '/chat' || /^\/chat\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(path) ? 'employee' : path === '/knowledge' || path === '/knowledge/upload' || /^\/knowledge\/documents\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(path) ? 'knowledge_manager' :
    path==='/organization' || /^\/organization\/members\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(path) ? 'tenant_admin' : null;
  return !!role && !!identity.memberships.find((m) => m.membership_id === identity.active_membership_id)?.roles.some((r) => r.role === role);
}

// Memory-only owner. No cookie/token access, persistence, network logging or automatic write replay.
export class AuthController {
  #api: ApiCoordinator; #state: AuthState = Object.freeze({ kind: 'booting' });
  #listeners = new Set<() => void>(); #load?: Promise<void>; #changing = false; #disposed = false;
  #unsubscribe: () => void; #broadcast?: () => void;
  #resume?: Promise<void>; #resumeVersion = 0;
  constructor(api: ApiCoordinator, broadcast?: () => void) {
    this.#api = api; this.#broadcast = broadcast;
    this.#unsubscribe = api.subscribeInvalidation(() => this.#set({ kind: this.#changing ? 'pending' : 'invalid',
      ...(this.#state.unknown ? { unknown: true, error: this.#state.error } : {}) }));
  }
  getSnapshot = (): AuthState => this.#state;
  subscribe = (listener: () => void): (() => void) => { this.#listeners.add(listener); return () => { this.#listeners.delete(listener); }; };
  #set(state: AuthState): void { if (!this.#disposed) { this.#state = Object.freeze(state); this.#listeners.forEach((f) => f()); } }
  #announce(): void { if (!this.#disposed) { try { this.#broadcast?.(); } catch { /* Signals are hints; fresh server checks remain authoritative. */ } } }
  refresh = (): Promise<void> => {
    if (this.#disposed || this.#changing) return Promise.resolve();
    if (this.#load) return this.#load;
    this.#set({ kind: 'booting' });
    const work = this.#api.bootstrap().then((stamp) => {
      const identity = this.#api.identityView(stamp);
      this.#set({ kind: identity.memberships.length === 0 ? 'no_membership' : !identity.active_membership_id ? 'needs_tenant' : 'ready', identity, stamp });
    }).catch((error: unknown) => {
      const kind = error instanceof ApiFailure ? error.kind : 'unavailable';
      this.#set({ kind: kind === 'unauthenticated' ? 'signed_out' : kind === 'context_changed' || kind === 'cancelled' ? 'invalid' : 'unavailable', error: kind });
    }).finally(() => { if (this.#load === work) this.#load = undefined; });
    this.#load = work; return work;
  };
  // Coalesce actual resume events, not errors. Wait out an aborted bootstrap then check the newest context.
  resume = (visible: () => boolean): Promise<void> => {
    this.#resumeVersion++;
    if (this.#resume) return this.#resume;
    const work = (async () => {
      let observed: number;
      do {
        observed = this.#resumeVersion;
        if (this.#load) await this.#load;
        if (!visible() || this.#changing || this.#state.unknown || this.#disposed) return;
        if (this.#state.kind === 'invalid' || this.#state.kind === 'booting') await this.refresh();
      } while (observed !== this.#resumeVersion);
    })().finally(() => { if (this.#resume === work) this.#resume = undefined; });
    this.#resume = work; return work;
  };
  async change(change: SessionChange): Promise<void> {
    const stamp = this.#state.stamp;
    if (!stamp || this.#changing || this.#load || this.#disposed) return;
    this.#changing = true; this.#set({ kind: 'pending' });
    try {
      await this.#api.changeSession(change, stamp);
      this.#announce();
      this.#changing = false;
      if (change.kind === 'logout') this.#set({ kind: 'signed_out' });
      else await this.refresh();
    } catch (error) {
      this.#announce();
      const failure = error instanceof ApiFailure ? error : new ApiFailure('unavailable');
      this.#set({ kind: 'invalid', error: failure.kind, unknown: failure.outcome === 'unknown' });
    } finally { this.#changing = false; }
  }
  dispose(): void { this.#disposed = true; this.#unsubscribe(); this.#listeners.clear(); this.#api.dispose(); }
}
