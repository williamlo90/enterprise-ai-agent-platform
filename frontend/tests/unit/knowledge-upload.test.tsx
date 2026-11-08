import { webcrypto } from 'node:crypto';
import { StrictMode } from 'react';
import { act,cleanup,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter } from 'react-router';
import { afterEach,beforeEach,expect,it,vi } from 'vitest';
import { App } from '../../src/app/App';
import { AuthScreen } from '../../src/app/AuthScreen';
import { AuthController } from '../../src/app/auth-controller';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { fileMime,fileDecoder,processingDecoder } from '../../src/features/upload-api';
import { maximumUploadBytes } from '../../src/api/upload-policy';
import { fixture,json,tenant,requestId } from './api-fixtures';

const id='77777777-7777-4777-8777-777777777777';const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const markdown='# Policy';const uploaded={id,purpose:'knowledge',mime:'text/markdown',size_bytes:markdown.length,scan_state:'quarantine',version:1};
const disposers:(()=>void)[]=[];
beforeEach(() => {
  vi.stubGlobal('crypto',webcrypto);
  // jsdom File implements browser selection but not Blob.arrayBuffer yet.
  Object.defineProperty(Blob.prototype,'arrayBuffer',{configurable:true,value:function(this:Blob) {return new Promise<ArrayBuffer>((resolve,reject) => {const reader=new FileReader();reader.onload=() => resolve(reader.result as ArrayBuffer);reader.onerror=() => reject(new Error('fixture read'));reader.readAsArrayBuffer(this);});}});
});
afterEach(() => {disposers.splice(0).forEach((dispose) => dispose());localStorage.clear();});
function identity(role='knowledge_manager') {const value=fixture();value.memberships[0].roles=[{role,scope_kind:'tenant',scope_ref:value.active_tenant_id}];return value;}
function normal(path:string,init:RequestInit) {
  if (path.endsWith('/me')) return json(identity());
  if (path.startsWith('/api/v1/knowledge/collections')) return json({items:[],next_after:null,tenant_level_allowed:true},200,binding);
  if (path.startsWith('/api/v1/files') && init.method==='POST') return json(uploaded,202,binding);
  if (path.endsWith('/status')) return json({...uploaded,scan_state:'ready',version:2},200,binding);
  return json({items:[],next_after:null},200,binding);
}
function setup(fetch:FetchPort=async (path,init) => normal(path,init)) {
  let tail:Promise<unknown>=Promise.resolve();const api=new ApiCoordinator(new ApiTransport(fetch),{request:async (_name,_options,work) => {const next=tail.then(work);tail=next.catch(() => {});return next;}});const auth=new AuthController(api);
  const router=createMemoryRouter([{path:'*',element:<AuthScreen auth={auth} api={api} />}],{initialEntries:['/knowledge/upload']});render(<StrictMode><App router={router} /></StrictMode>);disposers.push(() => {router.dispose();auth.dispose();});return {api,router};
}
async function choose() {
  const user=userEvent.setup();await screen.findByRole('option',{name:'Dokumen tingkat perusahaan'});
  await user.selectOptions(screen.getByRole('combobox',{name:'Cakupan dokumen'}),'tenant');
  await user.upload(screen.getByLabelText('File pengetahuan'),new File([markdown],'Kebijakan perjalanan nama lengkap.md',{type:'text/markdown'}));return user;
}
it('uploads Markdown with original bytes, shows server ready without creating a document or persisting data',async () => {
  const writes:{path:string;init:RequestInit}[]=[];setup(async (path,init) => {if (init.method==='POST') writes.push({path,init});return normal(path,init);});
  const user=await choose();expect(screen.getByText(/Kebijakan perjalanan nama lengkap.md/)).toBeTruthy();await user.click(screen.getByRole('button',{name:'Unggah file'}));
  await screen.findByRole('heading',{name:'File siap untuk pembuatan draft'});expect(screen.getByText('File telah lolos pemeriksaan. File ini belum menjadi dokumen dan belum diterbitkan.')).toBeTruthy();
  expect(writes).toHaveLength(1);expect(writes[0].path).toBe('/api/v1/files?purpose=knowledge');expect(writes[0].init.headers).toMatchObject({'Content-Type':'text/markdown','X-EAI-Expected-Context':'a'.repeat(64)});
  expect(new TextDecoder().decode(await (writes[0].init.body as Blob).arrayBuffer())).toBe(markdown);expect(localStorage.length).toBe(0);expect(sessionStorage.length).toBe(0);
});
it('does not interpret upload 202 or a quarantined status as ready or published',async () => {
  setup(async (path,init) => path.endsWith('/status')?json(uploaded,200,binding):normal(path,init));const user=await choose();await user.click(screen.getByRole('button',{name:'Unggah file'}));
  await screen.findByRole('heading',{name:'File dalam karantina'});expect(screen.queryByRole('heading',{name:'File siap untuk pembuatan draft'})).toBeNull();expect(screen.queryByRole('progressbar')).toBeNull();
});
it('shows rejected and unavailable status separately without parser retry or a success fallback',async () => {
  for (const rejected of [true,false]) {
    setup(async (path,init) => path.endsWith('/status')?rejected?json({...uploaded,scan_state:'rejected',version:2},200,binding):json({},404):normal(path,init));const user=await choose();await user.click(screen.getByRole('button',{name:'Unggah file'}));
    await screen.findByRole('heading',{name:rejected?'File ditolak':'Tidak tersedia'});expect(screen.queryByRole('heading',{name:'File siap untuk pembuatan draft'})).toBeNull();cleanup();
  }
});
it('retains one unknown upload across reentry and reconciles the identical key and Blob explicitly',async () => {
  const writes:RequestInit[]=[];setup(async (path,init) => {if (init.method==='POST') {writes.push(init);if (writes.length===1) throw new TypeError('controlled lost reply');}return normal(path,init);});
  const user=await choose();await user.click(screen.getByRole('button',{name:'Unggah file'}));await screen.findByRole('heading',{name:'Hasil unggahan belum diketahui'});
  await user.click(screen.getByRole('link',{name:'Kembali ke dokumen'}));await user.click(screen.getByRole('button',{name:'Buang perubahan dan lanjut'}));await user.click(await screen.findByRole('link',{name:'Unggah file'}));
  await screen.findByRole('heading',{name:'Hasil unggahan belum diketahui'});expect((screen.getByLabelText('File pengetahuan') as HTMLInputElement).disabled).toBe(true);expect(writes).toHaveLength(1);
  await user.click(screen.getByRole('button',{name:'Periksa hasil unggahan'}));await screen.findByRole('heading',{name:'File siap untuk pembuatan draft'});
  expect(writes).toHaveLength(2);expect(writes[0].body).toBe(writes[1].body);expect((writes[0].headers as Record<string,string>)['Idempotency-Key']).toBe((writes[1].headers as Record<string,string>)['Idempotency-Key']);
});
it('reentry observes a late dispatch failure and context invalidation clears all upload views',async () => {
  let reject!:(error:Error)=>void;const view=setup(async (path,init) => init.method==='POST'?new Promise((_resolve,rejecter) => {reject=rejecter;}):normal(path,init));
  const user=await choose();await user.click(screen.getByRole('button',{name:'Unggah file'}));await waitFor(() => expect(reject).toBeTypeOf('function'));
  await user.click(screen.getByRole('link',{name:'Kembali ke dokumen'}));await user.click(screen.getByRole('button',{name:'Buang perubahan dan lanjut'}));
  // The coordinator's exclusive lock holds catalog reads until the write settles.
  await user.click(screen.getByRole('link',{name:'Unggah file'}));await act(async () => {reject(new TypeError('controlled lost reply'));});
  await screen.findByRole('heading',{name:'Hasil unggahan belum diketahui'});act(() => view.api.invalidate());expect(screen.queryByRole('heading',{name:'Hasil unggahan belum diketahui'})).toBeNull();expect(screen.queryByLabelText('File pengetahuan')).toBeNull();
});
it('blocks employee and admin direct routes and does not offer company scope to collection-only discovery',async () => {
  for (const role of ['employee','tenant_admin']) {const calls:string[]=[];setup(async (path) => {calls.push(path);return json(identity(role));});await screen.findByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda'});expect(calls.every((path) => path==='/api/v1/me')).toBe(true);cleanup();}
  setup(async (path,init) => path.includes('/collections')?json({items:[{collection_id:tenant,display_name:null}],next_after:null,tenant_level_allowed:false},200,binding):normal(path,init));await screen.findByRole('option',{name:tenant});expect(screen.queryByRole('option',{name:'Dokumen tingkat perusahaan'})).toBeNull();
});
it('validates bounded file types and exact response bindings instead of displaying malformed metadata',() => {
  expect(fileMime({name:'policy.md',type:'text/plain',size:12})).toBe('text/markdown');expect(fileMime({name:'policy.txt',type:'',size:12})).toBe('text/plain');
  for (const change of [{size:0},{size:maximumUploadBytes+1},{name:'photo.png'},{type:'text/html'}]) expect(() => fileMime({name:'policy.md',type:'text/markdown',size:12,...change})).toThrow();
  const decode=fileDecoder({id,mime:'text/markdown',size:markdown.length,version:1});expect(decode(uploaded).scan_state).toBe('quarantine');
  for (const change of [{id:tenant},{mime:'text/plain'},{purpose:'receipt'},{size_bytes:1},{scan_state:'done'},{version:0}]) expect(() => decode({...uploaded,...change})).toThrow();
  const processing=processingDecoder(id,tenant,1);const status={document_id:id,version_id:tenant,version_no:1,document_version:3,parse_state:'ready',index_state:'building',publication_state:'draft'};
  expect(processing(status).index_state).toBe('building');expect(() => processing({...status,version_id:id})).toThrow();expect(() => processing({...status,parse_state:'success'})).toThrow();
});
it('clears status on offline instead of retaining the last private result',async () => {
  setup(async (path,init) => path.endsWith('/status')?json(uploaded,200,binding):normal(path,init));const user=await choose();await user.click(screen.getByRole('button',{name:'Unggah file'}));await screen.findByRole('heading',{name:'File dalam karantina'});
  act(() => window.dispatchEvent(new Event('offline')));expect(screen.queryByRole('heading',{name:'File dalam karantina'})).toBeNull();expect(screen.getByText(/Pemeriksaan berhenti saat halaman/)).toBeTruthy();
});
it('does not render untrusted server error text on rejected upload',async () => {
  setup(async (path,init) => init.method==='POST'?json({error:{code:'invalid',message:'PRIVATE RAW ERROR',retryable:false,request_id:requestId}},422):normal(path,init));const user=await choose();await user.click(screen.getByRole('button',{name:'Unggah file'}));await screen.findByRole('heading',{name:'Unggahan belum selesai'});expect(screen.queryByText('PRIVATE RAW ERROR')).toBeNull();
});
