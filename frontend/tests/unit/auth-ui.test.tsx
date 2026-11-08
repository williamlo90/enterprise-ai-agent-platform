import { StrictMode, useState } from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { AuthScreen } from '../../src/app/AuthScreen';
import type { AuthPages } from '../../src/app/AuthScreen';
import { AuthController } from '../../src/app/auth-controller';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { useUnsavedInput } from '../../src/app/unsaved-context';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { fixture, json, otherMembership, otherTenant } from './api-fixtures';

const disposers: (() => void)[] = [];
beforeEach(() => localStorage.clear());
afterEach(() => { disposers.splice(0).forEach((f) => f()); });
function setup(fetch: FetchPort, path = '/', pages?: AuthPages) {
  const api = new ApiCoordinator(new ApiTransport(fetch), { request: async (_n, _o, action) => action() });
  const auth = new AuthController(api);
  const router = createMemoryRouter([{ path: '*', element: <AuthScreen auth={auth} pages={pages} /> }], { initialEntries: [path] });
  render(<StrictMode><App router={router} /></StrictMode>);
  disposers.push(() => { router.dispose(); auth.dispose(); });
  return { api, auth, router };
}
it('offers only same-origin backend login navigation, without a password or OIDC browser handling', async () => {
  setup(async () => json({}, 401), '/chat');
  const link = await screen.findByRole('link', { name: 'Masuk melalui akun perusahaan' });
  expect(link.getAttribute('href')).toBe('/api/v1/auth/login?return_to=home');
  expect(document.querySelector('input[type=password]')).toBeNull(); expect(localStorage.length).toBe(0);
});
it('displays a no-membership state without inventing company access', async () => {
  setup(async () => json({ ...fixture(), memberships: [], active_tenant_id: null, active_membership_id: null, access_revision: null }));
  await screen.findByRole('heading', { name: 'Belum ada akses perusahaan' });
  expect(screen.queryByRole('link', { name: 'Percakapan' })).toBeNull(); expect(screen.getByRole('button', { name: 'Keluar' })).toBeTruthy();
});
it('selects only a membership_id and navigates to the current role landing after switching', async () => {
  let data = { ...fixture(), active_tenant_id: null as string | null, active_membership_id: null as string | null, access_revision: null as number | null };
  let body = '';
  const { router } = setup(async (path, init) => {
    if (path.endsWith('/session/tenant')) {
      body = String(init.body); data = { ...fixture(), active_tenant_id: otherTenant, active_membership_id: otherMembership, context_binding: 'b'.repeat(64) };
      data.memberships[1].roles = [{ role: 'knowledge_manager', scope_kind: 'tenant', scope_ref: otherTenant }];
    }
    return json(data);
  }, '/tenants');
  const user = userEvent.setup(); await screen.findByLabelText('Perusahaan');
  await user.selectOptions(screen.getByLabelText('Perusahaan'), otherMembership);
  await user.click(screen.getByRole('button', { name: 'Gunakan perusahaan' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/knowledge'));
  expect(body).toBe(JSON.stringify({ membership_id: otherMembership })); expect(screen.queryByRole('link', { name: 'Percakapan' })).toBeNull();
});
it('guards direct routes and never treats administrator as employee', async () => {
  const data = fixture(); data.memberships[0].roles = [{ role: 'tenant_admin', scope_kind: 'tenant', scope_ref: data.active_tenant_id }];
  setup(async () => json(data), '/chat'); await screen.findByRole('heading', { name: 'Halaman tidak tersedia untuk akses Anda' });
  expect(screen.queryByRole('link', { name: 'Percakapan' })).toBeNull();
});
it('renders the chosen two-area layout honestly without fabricated documents or messages', async () => {
  setup(async () => json(fixture()), '/chat'); await screen.findByRole('heading', { name: 'Fitur ini sedang disiapkan' });
  expect(screen.getByRole('complementary', { name: 'Sumber jawaban' })).toBeTruthy();
  expect(document.body.textContent).not.toContain('csrf_token'); expect(document.body.textContent).not.toContain('Kebijakan Cuti 2026');
});
function Draft() {
  const [text, setText] = useState(''); useUnsavedInput(!!text);
  return <label>Pesan belum dikirim<input value={text} onChange={(e) => setText(e.target.value)} /></label>;
}
it('supports stay/discard for route and logout, and invalidation clears input without confirmation', async () => {
  let writes = 0; const data = fixture(); data.memberships[0].roles.push({ role: 'knowledge_manager', scope_kind: 'tenant', scope_ref: data.active_tenant_id });
  const { api, router } = setup(async (path) => { if (path.endsWith('/logout')) { writes++; return new Response(null, { status: 204 }); } return json(data); }, '/chat', { '/chat': <Draft />, '/knowledge': <p>Katalog belum disiapkan</p> });
  const user = userEvent.setup(); await screen.findByLabelText('Pesan belum dikirim'); await user.type(screen.getByLabelText('Pesan belum dikirim'), 'Input privat');
  await user.click(screen.getByRole('link', { name: 'Dokumen' }));
  await screen.findByRole('dialog', { name: 'Ada perubahan yang belum disimpan' });
  await user.click(screen.getByRole('button', { name: 'Tetap di sini' })); expect(router.state.location.pathname).toBe('/chat');
  expect((screen.getByLabelText('Pesan belum dikirim') as HTMLInputElement).value).toBe('Input privat');
  await user.click(screen.getByRole('link', { name: 'Enterprise AI Agent Platform' }));
  await screen.findByRole('dialog', { name: 'Ada perubahan yang belum disimpan' });
  await user.click(screen.getByRole('button', { name: 'Tetap di sini' })); expect(router.state.location.pathname).toBe('/chat');
  await user.click(screen.getByRole('button', { name: 'Keluar' })); expect(writes).toBe(0);
  await user.click(screen.getByRole('button', { name: 'Tetap di sini' }));
  await act(async () => api.invalidate()); expect(screen.queryByLabelText('Pesan belum dikirim')).toBeNull(); expect(screen.queryByRole('dialog')).toBeNull();
  await user.click(screen.getByRole('button', { name: 'Periksa sesi lagi' })); await screen.findByLabelText('Pesan belum dikirim');
  expect((screen.getByLabelText('Pesan belum dikirim') as HTMLInputElement).value).toBe('');
  await user.type(screen.getByLabelText('Pesan belum dikirim'), 'Buang');
  await user.click(screen.getByRole('link', { name: 'Dokumen' })); await user.click(screen.getByRole('button', { name: 'Buang perubahan dan lanjut' }));
  await waitFor(() => expect(router.state.location.pathname).toBe('/knowledge')); expect(writes).toBe(0);
});
it('reports unknown logout, retains no private view and reconciles without resending POST', async () => {
  let posts = 0; const { auth } = setup(async (path) => { if (path.endsWith('/logout')) { posts++; return json({}, 503); } return json(fixture()); }, '/chat');
  const user = userEvent.setup(); await screen.findByRole('button', { name: 'Keluar' });
  await user.click(screen.getByRole('button', { name: 'Keluar' }));
  await screen.findByText(/Hasil permintaan belum dapat dipastikan/); expect(auth.getSnapshot().identity).toBeUndefined();
  await user.click(screen.getByRole('button', { name: 'Periksa sesi lagi' })); await screen.findByRole('button', { name: 'Keluar' }); expect(posts).toBe(1);
});
it('keeps appearance public and supports English auth labels with enum-only preferences', async () => {
  setup(async () => json({}, 401), '/appearance'); const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText('Bahasa antarmuka'), 'en'); await user.selectOptions(screen.getByLabelText('Theme'), 'dark');
  await user.click(screen.getByRole('link', { name: 'Back to workspace' }));
  await screen.findByRole('link', { name: 'Sign in with your company account' });
  expect(Object.keys(localStorage).sort()).toEqual(['eai.ui.locale', 'eai.ui.theme']);
});
