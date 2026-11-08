import { act,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach,expect,it,vi } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { PreferenceProvider } from '../../src/app/PreferenceProvider';
import { WorkspaceProvider } from '../../src/features/workspace';
import { DocumentAudience } from '../../src/features/DocumentAudience';
import { AudienceSelection } from '../../src/features/AudienceSelection';
import { fixture,json } from './api-fixtures';

const did='77777777-7777-4777-8777-777777777777';const sid='88888888-8888-4888-8888-888888888888';const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const id=(n:number) => '11111111-1111-4111-8111-'+String(n).padStart(12,'0');
const person=(n:number) => ({principal_id:id(n),membership_id:id(n),display_name:'Pembaca '+n,principal_active:true,membership_active:true,eligible:true});
const receipt={selection_id:sid,selected_count:51,snapshot_digest:'a'.repeat(64),expires_at:Date.now()/1000+900,creator_grant_included:false,base_document_id:did,expected_document_version:3,collection_id:null,retain_existing:true};
const readers=Array.from({length:51},(_,n) => person(n+1));
const current={document_id:did,document_version:3,access_revision:3,audience_mode:'specific_people',explicit_readers_count:1,readers:[person(1)],next_after:null,other_authorities:['knowledge_manager'],dynamic_employee_role:false};
const disposers:(()=>void)[]=[];
afterEach(() => disposers.splice(0).forEach((dispose) => dispose()));
function normal(path:string) {
  if (path.endsWith('/me')) return json(fixture());
  if (path.includes('/audience')) return json(current,200,binding);
  if (path.includes('/directory')) return json({items:readers.slice(0,50),next_after:id(50)},200,binding);
  if (path.includes('/selections/')) return json({...receipt,items:path.includes('after=')?readers.slice(50):readers.slice(0,50),next_after:path.includes('after=')?null:id(50),consumed:false},200,binding);
  return json(receipt,201,binding);
}
async function setup(fetch:FetchPort=async path => normal(path),admission=false) {
  let tail:Promise<unknown>=Promise.resolve();const api=new ApiCoordinator(new ApiTransport(fetch),{request:async (_name,_options,work) => {const next=tail.then(work);tail=next.catch(() => {});return next;}});const stamp=await api.bootstrap();const changed=vi.fn();
  render(<PreferenceProvider><WorkspaceProvider api={api} stamp={stamp}>{admission?<AudienceSelection collectionId={null} base={null} onReady={changed} />:<DocumentAudience documentId={did} collectionId={null} onChanged={changed} />}</WorkspaceProvider></PreferenceProvider>);disposers.push(() => api.dispose());
  const user=userEvent.setup();if (!admission) {await user.click(screen.getByRole('button',{name:'Periksa dan kelola pembaca'}));await screen.findByText('Audience saat ini: Orang tertentu');await user.click(screen.getByRole('button',{name:'Ubah audience'}));}
  return {api,user,changed};
}
it('freezes all results across pages and requires explicit review before enabling apply',async () => {
  const {user}=await setup();await screen.findByRole('checkbox',{name:'Pembaca 2'});await user.click(screen.getByRole('button',{name:'Bekukan semua hasil filter'}));await screen.findByRole('button',{name:'Pilih semua 51 orang'});
  expect((screen.getByRole('button',{name:'Simpan audience'}) as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByRole('button',{name:'Pilih semua 51 orang'}));await user.click(screen.getByRole('checkbox',{name:'Saya telah memeriksa audience dan dampaknya pada seluruh versi'}));expect((screen.getByRole('button',{name:'Simpan audience'}) as HTMLButtonElement).disabled).toBe(false);
  const changes=screen.getAllByRole('button',{name:'Nama berikutnya'});await user.click(changes.at(-1)!);await user.click(screen.getAllByRole('button',{name:'Nama berikutnya'}).at(-1)!);expect(screen.getAllByText('Pembaca 51').length).toBeGreaterThan(0);
});
it('changing filters preserves the frozen choice and manual removal requires a new summary',async () => {
  const {user}=await setup();await screen.findByRole('checkbox',{name:'Pembaca 2'});await user.click(screen.getByRole('checkbox',{name:'Pembaca 2'}));await user.type(screen.getByRole('textbox',{name:'Departemen'}),'Finance');await user.type(screen.getByRole('textbox',{name:'Jabatan'}),'Manager');await user.click(screen.getByRole('button',{name:'Terapkan filter pembaca'}));
  await waitFor(() => expect((screen.getByRole('checkbox',{name:'Pembaca 2'}) as HTMLInputElement).checked).toBe(true));expect(screen.getByText('Jumlah pembaca: 2')).toBeTruthy();
  await user.click(screen.getByRole('button',{name:'Hapus pembaca Pembaca 2'}));expect((screen.getByRole('button',{name:'Simpan audience'}) as HTMLButtonElement).disabled).toBe(true);
});
it('reconciles the original apply after a lost reply and refetches current audience',async () => {
  const bodies:unknown[]=[];let committed=false;
  const {user}=await setup(async (path,init) => {
    if (init.method==='PATCH') {bodies.push(init.body);if (!committed) {committed=true;throw new TypeError('controlled lost reply');}return json({document_id:did,document_version:4,access_revision:4,audience_mode:'all_current_employees',explicit_readers_count:0,selection_id:null},200,binding);}
    if (path.includes('/audience') && committed) return json({...current,document_version:8,access_revision:8,audience_mode:'all_current_employees',explicit_readers_count:0,readers:[],dynamic_employee_role:true},200,binding);return normal(path);
  });
  await user.selectOptions(screen.getByRole('combobox',{name:'Siapa yang boleh membaca'}),'all_current_employees');await user.click(screen.getByRole('checkbox',{name:'Saya telah memeriksa audience dan dampaknya pada seluruh versi'}));await user.click(screen.getByRole('button',{name:'Simpan audience'}));await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});
  await user.click(screen.getByRole('button',{name:'Periksa hasil penyimpanan'}));await screen.findByText('Audience saat ini: Seluruh karyawan');expect(bodies).toHaveLength(2);expect(bodies[0]).toBe(bodies[1]);
});
it('admission summary shows the server-added creator and invalidation cannot deliver a usable receipt',async () => {
  const admission={...receipt,base_document_id:null,expected_document_version:null,selected_count:1,creator_grant_included:true,retain_existing:false};
  const {api,user,changed}=await setup(async (path) => path.endsWith('/me')?json(fixture()):path.includes('/directory')?normal(path):path.includes('/selections/')?json({...admission,items:[person(1)],next_after:null,consumed:false},200,binding):json(admission,201,binding),true);
  await user.click(screen.getByRole('button',{name:'Periksa pilihan akhir'}));await screen.findByText('Pembuat dokumen ditambahkan server sebagai pembaca awal. Grant ini dapat dicabut kemudian.');
  act(() => api.invalidate());expect(screen.queryByRole('button',{name:'Gunakan pilihan 1 orang'})).toBeNull();
  // Production Workspace is unmounted by AuthController invalidation. The
  // standalone reusable consumer must also reject stale ready callbacks.
  expect(changed.mock.calls.every(([value]) => value===null)).toBe(true);
});
