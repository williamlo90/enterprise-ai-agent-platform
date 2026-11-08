import { useEffect, useState, useSyncExternalStore } from 'react';
import type { ReactNode } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import type { AuthController } from './auth-controller';
import { canOpen, landing } from './auth-controller';
import type { AuthState } from './auth-controller';
import { NavigationBoundary } from './unsaved-input';
import { usePreferences } from './preference-context';
import { authMessages } from '../locales/auth';
import { messages } from '../locales/messages';
import { AppShell } from '../components/AppShell';
import { Button } from '../components/Button';
import { SelectField } from '../components/Fields';
import { StatusMessage } from '../components/StatusMessage';
import { ReadingLayout } from '../components/ReadingLayout';
import { AppearancePage } from './App';
import type { ApiCoordinator } from './api-coordinator';
import { WorkspaceProvider } from '../features/workspace';
import { OrganizationDirectory,OrganizationDetail } from '../features/OrganizationPage';
import { KnowledgeCatalog,KnowledgeDetail } from '../features/KnowledgePage';
import { KnowledgeUpload } from '../features/KnowledgeUpload';
import { SourceSearch } from '../features/SourceSearch';
import { SourcePage } from '../features/SourceWorkspace';
import {Approvals,Tasks} from '../features/reimbursement/ClaimsWorkspace';
import {Administration} from '../features/admin/Administration';
import { Reimbursements } from '../features/reimbursement/Reimbursements';
import { ConversationList,ConversationPage } from '../features/chat/Conversations';

export type AuthPages = Partial<Record<'/chat' | '/knowledge', ReactNode>>;
function TenantPicker({ state, auth }: { state: AuthState; auth: AuthController }) {
  const { locale } = usePreferences(); const c = authMessages[locale];
  const navigate = useNavigate();
  const [selected, setSelected] = useState(state.identity?.active_membership_id ?? state.identity?.memberships[0]?.membership_id ?? '');
  return <section className="auth-panel"><h1>{c.choose}</h1><p>{c.chooseHint}</p>
    <form onSubmit={(event) => { event.preventDefault(); if (selected) void auth.change({ kind: 'switch', membershipId: selected }).then(() => {
      const next = auth.getSnapshot(); if (next.kind === 'ready' && next.identity) void navigate(landing(next.identity), { replace: true });
    }); }}>
      <SelectField label={c.company} value={selected} onChange={(e) => setSelected(e.target.value)}
        options={(state.identity?.memberships ?? []).map((m) => ({ value: m.membership_id, label: m.tenant_name }))} />
      <Button type="submit" variant="primary" disabled={!selected}>{c.continue}</Button>
    </form>
  </section>;
}
export function AuthScreen({ auth, pages, api }: { auth: AuthController; pages?: AuthPages; api?:ApiCoordinator }) {
  const state = useSyncExternalStore(auth.subscribe, auth.getSnapshot);
  const { locale } = usePreferences(); const c = authMessages[locale]; const base = messages[locale];
  const location = useLocation(); const navigate = useNavigate();
  useEffect(() => { void auth.refresh(); }, [auth]);
  useEffect(() => {
    if (state.kind === 'ready' && state.identity && location.pathname === '/') void navigate(landing(state.identity), { replace: true });
  }, [state, location.pathname, navigate]);
  const appearance = location.pathname === '/appearance';
  const identity = state.identity;
  const current = identity?.memberships.find((m) => m.membership_id === identity.active_membership_id);
  const labels = { ...base, publicScope: current?.tenant_name ?? c.access, publicView: current?.tenant_name ?? c.access };
  const ready = state.kind === 'ready' && !!identity;
  const navigation = <>{ready && canOpen(identity, '/chat') && <Link className="nav-link" to="/chat" aria-current={location.pathname === '/chat' ? 'page' : undefined}>{c.chat}</Link>}
    {ready && canOpen(identity,'/reimbursements') && <Link className="nav-link" to="/reimbursements" aria-current={location.pathname.startsWith('/reimbursements')?'page':undefined}>{locale==='id'?'Penggantian biaya':'Reimbursements'}</Link>}
    {ready&&canOpen(identity,'/tasks')&&<Link className="nav-link" to="/tasks">{locale==='id'?'Perlu Tindakan':'Action required'}</Link>}
    {ready&&canOpen(identity,'/approvals')&&<Link className="nav-link" to="/approvals">{locale==='id'?'Persetujuan':'Approvals'}</Link>}
    {ready&&canOpen(identity,'/admin')&&<Link className="nav-link" to="/admin">{locale==='id'?'Administrasi':'Administration'}</Link>}
    {ready&&canOpen(identity,'/operator')&&<Link className="nav-link" to="/operator">{locale==='id'?'Operasional':'Operations'}</Link>}
    {ready && canOpen(identity, '/search') && <Link className="nav-link" to="/search" aria-current={location.pathname==='/search'?'page':undefined}>{locale==='id'?'Cari sumber':'Find sources'}</Link>}
    {ready && canOpen(identity, '/knowledge') && <Link className="nav-link" to="/knowledge" aria-current={location.pathname.startsWith('/knowledge') ? 'page' : undefined}>{c.knowledge}</Link>}
    {ready && canOpen(identity, '/organization') && <Link className="nav-link" to="/organization" aria-current={location.pathname.startsWith('/organization') ? 'page' : undefined}>{locale==='id'?'Organisasi':'Organization'}</Link>}
    <Link className="nav-link" to="/appearance">{c.appearance}</Link></>;
  if (appearance) return <AppearancePage returnLink={<Link to="/">{c.return}</Link>} />;
  if (ready) return <NavigationBoundary key={state.stamp!.generation}>{(request) => <AppShell labels={labels} navigation={navigation}
    renderHomeLink={(children) => <Link className="brand" to="/" aria-label="Enterprise AI Agent Platform">{children}</Link>} actions={<>
    <Button onClick={() => request(() => { void navigate('/tenants'); })}>{c.change}</Button>
    <Button onClick={() => request(() => { void auth.change({ kind: 'logout' }); })}>{c.signOut}</Button>
  </>}>
    {location.pathname === '/tenants' ? <TenantPicker state={state} auth={auth} /> : !canOpen(identity, location.pathname) ?
      <StatusMessage kind="error" title={c.denied}><p>{c.deniedHint}</p></StatusMessage> : location.pathname === '/access' ?
      <StatusMessage kind="empty" title={c.noRole}><p>{c.noRoleHint}</p></StatusMessage> : api&&(location.pathname==='/tasks'||location.pathname.startsWith('/approvals'))?<WorkspaceProvider api={api} stamp={state.stamp!}>{location.pathname==='/tasks'?<Tasks/>:<Approvals key={location.pathname} id={location.pathname.split('/')[2]}/>}</WorkspaceProvider>:api&&(location.pathname.startsWith('/admin')||location.pathname.startsWith('/operator'))?<WorkspaceProvider api={api} stamp={state.stamp!}><Administration key={location.pathname} path={location.pathname}/></WorkspaceProvider>:api && location.pathname.startsWith('/reimbursements') ?
      <WorkspaceProvider api={api} stamp={state.stamp!}><Reimbursements key={location.pathname+location.search} path={location.pathname} search={location.search}/></WorkspaceProvider> : api && (location.pathname==='/search' || location.pathname.startsWith('/sources/')) ?
      <WorkspaceProvider api={api} stamp={state.stamp!}>{location.pathname==='/search'?<SourceSearch />:<SourcePage key={location.pathname} versionId={location.pathname.split('/').at(-1)!} />}</WorkspaceProvider> : api && location.pathname.startsWith('/organization') ?
      <WorkspaceProvider api={api} stamp={state.stamp!}>{location.pathname==='/organization'?<OrganizationDirectory />:<OrganizationDetail key={location.pathname} membershipId={location.pathname.split('/').at(-1)!} />}</WorkspaceProvider> : api && (location.pathname==='/knowledge' || location.pathname==='/knowledge/upload' || location.pathname.startsWith('/knowledge/documents/')) && !pages?.['/knowledge'] ?
      <WorkspaceProvider api={api} stamp={state.stamp!}>{location.pathname==='/knowledge'?<KnowledgeCatalog />:location.pathname==='/knowledge/upload'?<KnowledgeUpload />:<KnowledgeDetail key={location.pathname} documentId={location.pathname.split('/').at(-1)!} />}</WorkspaceProvider> : api && (location.pathname==='/chat'||location.pathname.startsWith('/chat/'))&&!pages?.['/chat'] ?
      <WorkspaceProvider api={api} stamp={state.stamp!}>{location.pathname==='/chat'?<ConversationList/>:<ConversationPage key={location.pathname} id={location.pathname.split('/').at(-1)!}/>}</WorkspaceProvider> : pages?.[location.pathname as keyof AuthPages] ??
      (location.pathname === '/knowledge' ? <><div className="page-heading"><h1>{c.knowledge}</h1></div>
        <StatusMessage kind="empty" title={c.feature}><p>{c.featureHint}</p></StatusMessage></> :
      <ReadingLayout sourceLabel={c.source} sources={<><h2>{c.source}</h2><p className="field-hint">{c.sourceHint}</p></>}>
        <div className="page-heading"><h1>{location.pathname === '/knowledge' ? c.knowledge : c.chat}</h1></div>
        <StatusMessage kind="empty" title={c.feature}><p>{c.featureHint}</p></StatusMessage>
      </ReadingLayout>)}
    <p className="session-note">{c.signOutNote}</p>
  </AppShell>}</NavigationBoundary>;
  let content: ReactNode;
  if (state.kind === 'signed_out') content = <section className="auth-panel"><h1>{c.welcome}</h1><p>{c.intro}</p>
    <a className="button button--primary" href="/api/v1/auth/login?return_to=home" referrerPolicy="no-referrer">{c.signIn}</a></section>;
  else if (state.kind === 'no_membership' || state.kind === 'needs_tenant') content = <>
    {state.kind === 'no_membership' ? <StatusMessage kind="empty" title={c.noMembership}><p>{c.noMembershipHint}</p></StatusMessage> : <TenantPicker state={state} auth={auth} />}
    <Button onClick={() => { void auth.change({ kind: 'logout' }); }}>{c.signOut}</Button><p className="field-hint">{c.signOutNote}</p></>;
  else if (state.kind === 'booting' || state.kind === 'pending') content = <StatusMessage kind="loading" title={state.kind === 'pending' ? c.pending : c.loading}><p>{state.kind === 'pending' ? c.pendingHint : c.loadingHint}</p></StatusMessage>;
  else content = <section className="auth-access" aria-labelledby="access-heading">
    <header className="auth-access-heading"><h1 id="access-heading">{c.welcome}</h1><p>{c.accessHint}</p></header>
    <StatusMessage kind={state.error === 'not_found' && !state.unknown ? 'info' : 'error'} title={state.error === 'unsupported' ? c.unsupported : state.kind === 'invalid' ? c.invalid : state.error === 'not_found' ? c.serviceUnavailable : c.unavailable}>
      <p>{state.unknown ? c.unknown : state.error === 'unsupported' ? c.unsupportedHint : state.kind === 'invalid' ? c.invalidHint : state.error === 'not_found' ? c.serviceUnavailableHint : c.unavailableHint}</p>
    </StatusMessage>
    <div className="auth-access-actions"><Button onClick={() => { void auth.refresh(); }}>{c.check}</Button><Link to="/appearance">{c.appearanceAction}</Link></div>
  </section>;
  return <AppShell labels={labels} navigation={navigation}>{content}</AppShell>;
}
