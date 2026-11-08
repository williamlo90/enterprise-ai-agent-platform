import { StrictMode } from 'react';
import { act,cleanup,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter } from 'react-router';
import { afterEach,expect,it } from 'vitest';
import { App } from '../../src/app/App';
import { AuthScreen } from '../../src/app/AuthScreen';
import { AuthController } from '../../src/app/auth-controller';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { decodeCatalog,decodeDocument,decodeVersion,decodeVersions,documentDecoder } from '../../src/features/knowledge-api';
import { fixture,json,tenant,otherTenant,requestId } from './api-fixtures';

const did='77777777-7777-4777-8777-777777777777';const vid='88888888-8888-4888-8888-888888888888';const second='99999999-9999-4999-8999-999999999999';
const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const version={document_id:did,document_version:3,version_id:vid,version_no:1,parse_state:'ready',index_state:'ready',publication_state:'published',chunk_count:1,file_id:tenant,file_digest:'b'.repeat(64),content_digest:'c'.repeat(64),index_generation:1,created_at:'2026-11-05T00:00:00+00:00',valid_from:null,valid_until:null,error_code:null};
const draft={...version,version_id:second,version_no:2,parse_state:'pending',index_state:'pending',publication_state:'draft',chunk_count:null,index_generation:0,content_digest:null};
const document={document_id:did,title:'Kebijakan perjalanan',collection_id:null,document_version:3,access_revision:2,active_version_id:vid,created_at:version.created_at,active_version:version,latest_version:draft};
const disposers:(()=>void)[]=[];
afterEach(() => {disposers.splice(0).forEach((dispose) => dispose());localStorage.clear();});
function identity(role='knowledge_manager') {const value=fixture();value.memberships[0].roles=[{role,scope_kind:'tenant',scope_ref:value.active_tenant_id}];return value;}
function setup(fetch:FetchPort,path='/knowledge') {
  let tail:Promise<unknown>=Promise.resolve();const api=new ApiCoordinator(new ApiTransport(fetch),{request:async (_name,_options,work) => {const next=tail.then(work);tail=next.catch(() => {});return next;}});const auth=new AuthController(api);
  const router=createMemoryRouter([{path:'*',element:<AuthScreen auth={auth} api={api} />}],{initialEntries:[path]});render(<StrictMode><App router={router} /></StrictMode>);disposers.push(() => {router.dispose();auth.dispose();});return {api,router};
}
function normal(path:string) {
  if (path.endsWith('/me')) return json(identity());
  if (path.startsWith('/api/v1/knowledge/collections')) return json({items:[],next_after:null,tenant_level_allowed:true},200,binding);
  if (path.includes('/versions')) return json({document_id:did,document_version:3,items:[draft,version],next_after:null},200,binding);
  if (path==='/api/v1/knowledge/documents/'+did) return json(document,200,binding);
  return json({items:[document],next_after:null},200,binding);
}

it('discovers manager documents and separates active published version from the latest draft without history fan-out',async () => {
  const urls:string[]=[];setup(async (url) => {urls.push(url);return normal(url);});
  await screen.findByRole('link',{name:'Kebijakan perjalanan'});expect(screen.getByText('Aktif: versi 1')).toBeTruthy();expect(screen.getByText(/Terbaru: versi 2/)).toBeTruthy();
  expect(urls.some((url) => url.includes('/versions') || url.includes('/search'))).toBe(false);
  await userEvent.setup().click(screen.getByRole('link',{name:'Periksa dokumen'}));await screen.findByRole('heading',{name:'Riwayat versi'});await screen.findByRole('heading',{name:'Versi 2'});
  expect(screen.getByText('Versi aktif tetap digunakan sampai versi pengganti diterbitkan.')).toBeTruthy();expect(screen.getAllByText('Belum dicatat').length).toBeGreaterThan(0);
});

it('paginates only scoped collection choices and sends literal title/collection filters without URL persistence',async () => {
  const urls:string[]=[];const {router}=setup(async (url) => {
    urls.push(url);if (url.startsWith('/api/v1/knowledge/collections')) return json(url.includes('after=')?{items:[{collection_id:otherTenant,display_name:null}],next_after:null,tenant_level_allowed:false}:{items:[{collection_id:tenant,display_name:null}],next_after:tenant,tenant_level_allowed:false},200,binding);return normal(url);
  });const user=userEvent.setup();await screen.findByRole('option',{name:tenant});expect(screen.queryByRole('option',{name:'Dokumen tingkat perusahaan'})).toBeNull();
  await user.click(screen.getByRole('button',{name:'Koleksi berikutnya'}));await screen.findByRole('option',{name:otherTenant});await user.selectOptions(screen.getByRole('combobox',{name:'Cakupan dokumen'}),otherTenant);
  await user.type(screen.getByRole('textbox',{name:'Cari judul dokumen'}),'Perjalanan* & Cuti');await user.click(screen.getByRole('button',{name:'Terapkan filter'}));
  await waitFor(() => expect(urls.some((url) => {const params=new URL(url,'https://example.test').searchParams;return params.get('q')==='Perjalanan* & Cuti' && params.get('collection')===otherTenant;})).toBe(true));
  expect(router.state.location.search).toBe('');expect(localStorage.length).toBe(0);expect(sessionStorage.length).toBe(0);
});

it('distinguishes unavailable catalog from a successfully empty result and removes the prior list on filter changes',async () => {
  let failed=false;setup(async (url) => url.startsWith('/api/v1/knowledge/documents?') && failed?json({error:{code:'unavailable',message:'private',retryable:true,request_id:requestId}},503):normal(url));
  const user=userEvent.setup();await screen.findByRole('link',{name:'Kebijakan perjalanan'});failed=true;await user.type(screen.getByRole('textbox',{name:'Cari judul dokumen'}),'other');await user.click(screen.getByRole('button',{name:'Terapkan filter'}));
  await screen.findByRole('heading',{name:'Data belum dapat dimuat'});expect(screen.queryByRole('link',{name:'Kebijakan perjalanan'})).toBeNull();expect(screen.queryByRole('heading',{name:'Belum ada dokumen yang cocok'})).toBeNull();expect(screen.getByRole('button',{name:'Keluar'})).toBeTruthy();
});

it('discards version rows when their CAS differs from the document detail and permits a current reload',async () => {
  let current=false;setup(async (url) => url.includes('/versions')?json({document_id:did,document_version:current?3:4,items:[],next_after:null},200,binding):normal(url),'/knowledge/documents/'+did);
  await screen.findByRole('heading',{name:'Dokumen telah berubah'});expect(screen.queryByRole('heading',{name:'Versi 2'})).toBeNull();current=true;await userEvent.setup().click(screen.getByRole('button',{name:'Muat ulang dokumen'}));await screen.findByText('Belum ada versi.');
});

it('clears unavailable detail content and clears all private metadata on context invalidation',async () => {
  let missing=false;const {api}=setup(async (url) => url.endsWith('/documents/'+did) && missing?json({},404):normal(url),'/knowledge/documents/'+did);
  await screen.findByRole('heading',{name:'Kebijakan perjalanan'});missing=true;await userEvent.setup().click(screen.getByRole('button',{name:'Periksa status terbaru'}));await screen.findByRole('heading',{name:'Tidak tersedia'});expect(screen.queryByRole('heading',{name:'Kebijakan perjalanan'})).toBeNull();
  act(() => api.invalidate());expect(screen.queryByRole('heading',{name:'Riwayat versi'})).toBeNull();
});

it('does not grant manager catalog access to employees or tenant administrators',async () => {
  for (const role of ['employee','tenant_admin']) {
    const urls:string[]=[];const view=setup(async (url) => {urls.push(url);return json(identity(role));});await screen.findByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda'});
    expect(urls.every((url) => url==='/api/v1/me')).toBe(true);await act(async () => {view.router.dispose();view.api.dispose();});
    // Cleanup this render before the next independent identity fixture.
    cleanup();
  }
});

it('validates DTO relationships, ordering, duplicate IDs and real date boundaries',() => {
  expect(decodeDocument(document).active_version?.version_no).toBe(1);
  expect(() => documentDecoder(tenant)(document)).toThrow();expect(() => decodeDocument({...document,active_version:{...version,document_id:tenant}})).toThrow();
  expect(() => decodeCatalog({items:[document,document],next_after:null})).toThrow();expect(() => decodeVersions({document_id:did,document_version:3,items:[version,draft],next_after:null})).toThrow();
  expect(() => decodeVersion({...version,valid_from:'2026-02-30'})).toThrow();expect(() => decodeVersion({...version,valid_from:'2026-11-05',valid_until:'2026-11-05'})).toThrow();
  expect(() => decodeVersion({...version,index_state:'complete'})).toThrow();
});
