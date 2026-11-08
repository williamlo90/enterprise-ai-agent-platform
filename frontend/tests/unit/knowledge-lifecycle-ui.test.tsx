import { act,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter,useLocation } from 'react-router';
import { afterEach,expect,it,vi } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { App } from '../../src/app/App';
import { WorkspaceProvider } from '../../src/features/workspace';
import { KnowledgeAdmission } from '../../src/features/KnowledgeAdmission';
import { KnowledgeLifecycle } from '../../src/features/KnowledgeLifecycle';
import { PendingKnowledgeOperations } from '../../src/features/PendingKnowledgeOperations';
import { decodeDocument } from '../../src/features/knowledge-api';
import { fixture,json,tenant } from './api-fixtures';

const did='77777777-7777-4777-8777-777777777777';const vid='88888888-8888-4888-8888-888888888888';const fid='99999999-9999-4999-8999-999999999999';const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const version={document_id:did,document_version:3,version_id:vid,version_no:1,parse_state:'ready',index_state:'ready',publication_state:'draft',chunk_count:1,file_id:fid,file_digest:'a'.repeat(64),content_digest:'b'.repeat(64),index_generation:1,created_at:'2026-11-05T00:00:00Z',valid_from:null,valid_until:null,error_code:null};
const document=decodeDocument({document_id:did,title:'Policy',document_version:3,access_revision:2,collection_id:null,active_version_id:null,active_version:null,latest_version:version,created_at:version.created_at});
const creator={principal_id:tenant,membership_id:tenant,display_name:'Pembuat dokumen',principal_active:true,membership_active:true};
const selection={selection_id:vid,selected_count:1,snapshot_digest:'a'.repeat(64),expires_at:Date.now()/1000+900,creator_grant_included:true,base_document_id:null,expected_document_version:null,collection_id:null,retain_existing:false};
const admission={document_id:did,document_version:1,version_id:vid,version_no:1,parse_state:'pending',index_state:'pending',publication_state:'draft',valid_from:null,valid_until:null};
const text='<img src=x onerror=alert(1)> policy';
const preview={...version,title:'Policy',access_revision:2,preview:true,format:'extracted_chunks',chunks:[{chunk_no:0,page:1,start:0,end:text.length,text,text_digest:'c'.repeat(64)}]};
const disposers:(()=>void)[]=[];afterEach(() => {disposers.splice(0).forEach((dispose) => dispose());localStorage.clear();});
function normal(path:string,init:RequestInit) {
  if (path.endsWith('/me')) return json(fixture());
  if (path.includes('/directory')) return json({items:[],next_after:null},200,binding);
  if (path.includes('/selections/')) return json({...selection,items:[creator],next_after:null,consumed:false},200,binding);
  if (path.endsWith('/selections')) return json(selection,201,binding);
  if (path.endsWith('/preview')) return json(preview,200,binding);
  if (path.includes('/audience')) return json({document_id:did,document_version:3,access_revision:2,audience_mode:'specific_people',explicit_readers_count:0,readers:[],next_after:null,other_authorities:['knowledge_manager'],dynamic_employee_role:false},200,binding);
  if (init.method==='POST' && path.endsWith('/publish')) return json({document_id:did,document_version:4,access_revision:3,active_version_id:vid,withdrawn:false,operation:'publish',operation_version_id:vid,result_version:4,result_access_revision:3},200,binding);
  return json(admission,202,binding);
}
async function setup(fetch:FetchPort=async (path,init) => normal(path,init),lifecycle=false) {
  let tail:Promise<unknown>=Promise.resolve();const api=new ApiCoordinator(new ApiTransport(fetch),{request:async (_name,_options,work) => {const next=tail.then(work);tail=next.catch(() => {});return next;}});const stamp=await api.bootstrap();const refreshed=vi.fn();
  function Scene() {const location=useLocation();return <WorkspaceProvider api={api} stamp={stamp}>{location.pathname==='/admit'?<KnowledgeAdmission file={{id:fid,purpose:'knowledge',mime:'text/plain',size_bytes:10,scan_state:'ready',version:2}} collectionId={null} onAdmitted={refreshed} />:lifecycle?<KnowledgeLifecycle document={document} versions={[document.latest_version!]} refresh={refreshed} />:<PendingKnowledgeOperations />}</WorkspaceProvider>;}
  const router=createMemoryRouter([{path:'*',element:<Scene />}],{initialEntries:[lifecycle?'/review':'/admit']});render(<App router={router} />);disposers.push(() => {router.dispose();api.dispose();});return {api,router,refreshed,user:userEvent.setup()};
}
it('requires server creator review for named initial audience and uses a distinct admission intent from selection creation',async () => {
  const requests:{path:string;body:Record<string,unknown>}[]=[];const {user,refreshed}=await setup(async (path,init) => {if (init.method==='POST') requests.push({path,body:JSON.parse(init.body as string)});return normal(path,init);});
  await user.type(screen.getByRole('textbox',{name:'Judul dokumen'}),'Judul dokumen lengkap');expect((screen.getByRole('button',{name:'Buat draft'}) as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByRole('button',{name:'Periksa pilihan akhir'}));await screen.findByText('Pembuat dokumen ditambahkan server sebagai pembaca awal. Grant ini dapat dicabut kemudian.');await user.click(screen.getByRole('button',{name:'Gunakan pilihan 1 orang'}));await user.click(screen.getByRole('checkbox',{name:'Saya telah memeriksa judul, periode dan audience draft ini'}));await user.click(screen.getByRole('button',{name:'Buat draft'}));
  await screen.findByRole('heading',{name:'Draft diterima server'});expect(requests).toHaveLength(2);expect(requests[0].body.key).not.toBe(requests[1].body.key);expect(requests[1].body).toMatchObject({file_id:fid,title:'Judul dokumen lengkap',readers:[],audience_mode:'specific_people',selection_id:vid});expect(refreshed).toHaveBeenCalledTimes(1);
});
it('recovers unknown admission from the catalog after unmount using original file, title, dates, and key',async () => {
  const sent:unknown[]=[];const {user,router}=await setup(async (path,init) => {
    if (path==='/api/v1/knowledge/documents' && init.method==='POST') {sent.push(init.body);if (sent.length===1) throw new TypeError('lost committed admission fixture');}
    return normal(path,init);
  });await user.type(screen.getByRole('textbox',{name:'Judul dokumen'}),'Policy');await user.selectOptions(screen.getByRole('combobox',{name:'Audience awal dokumen'}),'all_current_employees');await user.click(screen.getByRole('checkbox',{name:'Saya telah memeriksa judul, periode dan audience draft ini'}));await user.click(screen.getByRole('button',{name:'Buat draft'}));await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});
  await act(async () => {await router.navigate('/catalog');});await screen.findByRole('heading',{name:'Hasil tindakan dokumen belum diketahui'});await user.click(screen.getByRole('button',{name:'Rekonsiliasi tindakan dokumen'}));await waitFor(() => expect(router.state.location.pathname).toBe('/knowledge/documents/'+did));expect(sent).toHaveLength(2);expect(sent[0]).toBe(sent[1]);
});
it('requires a fresh extracted preview and explicit review before publish, escaping source HTML as text',async () => {
  const {user,refreshed}=await setup(undefined,true);expect(screen.queryByRole('button',{name:'Terbitkan versi ini'})).toBeNull();await user.click(screen.getByRole('button',{name:'Buka preview ekstraksi'}));await screen.findByText(text);expect(globalThis.document.querySelector('img')).toBeNull();
  expect((screen.getByRole('button',{name:'Terbitkan versi ini'}) as HTMLButtonElement).disabled).toBe(true);await user.click(screen.getByRole('checkbox',{name:'Saya telah memeriksa ekstraksi, periode dan audience versi ini'}));await user.click(screen.getByRole('button',{name:'Terbitkan versi ini'}));await waitFor(() => expect(refreshed).toHaveBeenCalledTimes(1));expect(screen.queryByText(text)).toBeNull();
});
it('does not treat preview failure as publish readiness and purges extracted content on context invalidation',async () => {
  let unavailable=false;const {api,user}=await setup(async (path,init) => path.endsWith('/preview') && unavailable?json({},404):normal(path,init),true);
  await user.click(screen.getByRole('button',{name:'Buka preview ekstraksi'}));await screen.findByText(text);unavailable=true;await user.click(screen.getByRole('button',{name:'Buka preview ekstraksi'}));await screen.findByRole('heading',{name:'Preview tidak tersedia'});expect(screen.queryByText(text)).toBeNull();expect(screen.queryByRole('button',{name:'Terbitkan versi ini'})).toBeNull();act(() => api.invalidate());expect(screen.queryByText(text)).toBeNull();
});
