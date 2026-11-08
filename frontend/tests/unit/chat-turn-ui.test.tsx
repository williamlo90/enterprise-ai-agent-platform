import {StrictMode} from 'react';
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {createMemoryRouter} from 'react-router';
import {afterEach,expect,it} from 'vitest';
import {App} from '../../src/app/App';
import {AuthScreen} from '../../src/app/AuthScreen';
import {AuthController} from '../../src/app/auth-controller';
import {ApiCoordinator} from '../../src/app/api-coordinator';
import {ApiTransport} from '../../src/api/transport';
import type {FetchPort} from '../../src/api/transport';
import {fixture,json} from './api-fixtures';
import {sourceValue,sourceChunk,versionId} from './source-fixtures';
const c='77777777-7777-4777-8777-777777777777',r='88888888-8888-4888-8888-888888888888',m='99999999-9999-4999-8999-999999999999';
const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const conversation={id:c,language:'id',version:3,label:'Pertanyaan',label_source:'first_question',active_run_id:null as string|null,created_at:'2026-11-06T00:00:00Z',updated_at:'2026-11-06T00:00:00Z'};
const human={id:m,sequence:1,author_kind:'human',run_id:r,output_kind:null,text:'Pertanyaan baru',redacted:false,sources:[],citations:[],feedback:null};
const answer={...human,id:c,sequence:2,author_kind:'model',output_kind:'answer',text:'Jawaban terbaru'};
const disposers:(()=>void)[]=[];afterEach(()=>{cleanup();disposers.splice(0).forEach(d=>d());localStorage.clear();});
function setup(fetch:FetchPort){const api=new ApiCoordinator(new ApiTransport(fetch),{request:async(_n,_o,work)=>work()});const auth=new AuthController(api);const router=createMemoryRouter([{path:'*',element:<AuthScreen api={api} auth={auth}/>}],{initialEntries:['/chat/'+c]});render(<StrictMode><App router={router}/></StrictMode>);disposers.push(()=>{router.dispose();auth.dispose();});return api;}
it('keeps unknown admission immutable and admits once on explicit replay without inventing an answer',async()=>{
  const bodies:string[]=[];let admitted=false;
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(init.method==='POST'){bodies.push(init.body as string);admitted=true;if(bodies.length===1)throw new TypeError('controlled');const body=JSON.parse(bodies[0]);return json({conversation_id:c,client_message_id:body.client_message_id,message_id:m,sequence:1,run_id:r,run_state:'queued',conversation_version:4},202,binding);}
    if(path.includes('/runs/'))return json({id:r,conversation_id:c,state:'queued',version:1},200,binding);
    return json({conversation:{...conversation,active_run_id:admitted?r:null},messages:admitted?[human]:[],next_after:null},200,binding);
  });const user=userEvent.setup();await user.type(await screen.findByLabelText('Pertanyaan Anda'),'Pertanyaan baru');await user.click(screen.getByRole('button',{name:'Kirim pertanyaan'}));await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});expect((screen.getByLabelText('Pertanyaan Anda') as HTMLTextAreaElement).disabled).toBe(true);
  await user.click(screen.getByRole('button',{name:'Periksa hasil penyimpanan'}));await screen.findByText('Pertanyaan baru');expect(bodies).toHaveLength(2);expect(bodies[0]).toBe(bodies[1]);expect(JSON.parse(bodies[0]).key).toBeUndefined();expect(JSON.parse(bodies[0]).client_message_id).toMatch(/^[0-9a-f-]{36}$/);expect(screen.queryByRole('heading',{name:'Jawaban'})).toBeNull();
});
it('requires actual matching current-run history even beyond the first20 messages',async()=>{
  const calls:string[]=[];let admitted=false;
  setup(async(path,init)=>{
    calls.push(path);if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(init.method==='POST'){admitted=true;return json({conversation_id:c,client_message_id:JSON.parse(init.body as string).client_message_id,message_id:m,sequence:21,run_id:r,run_state:'completed',conversation_version:4},202,binding);}
    if(path.includes('/runs/'))return json({id:r,conversation_id:c,state:'completed',version:2},200,binding);
    const later=path.includes('after=20');return json({conversation,messages:admitted?(later?[{...human,sequence:21},{...answer,sequence:22}]:Array.from({length:20},(_,i)=>({...human,id:'10000000-0000-4000-8000-'+String(i+1).padStart(12,'0'),sequence:i+1}))):[],next_after:admitted&&!later?20:null},200,binding);
  });const user=userEvent.setup();await user.type(await screen.findByLabelText('Pertanyaan Anda'),'Pertanyaan baru');await user.click(screen.getByRole('button',{name:'Kirim pertanyaan'}));await screen.findByText('Jawaban terbaru');expect(calls.some(p=>p.includes('after=20'))).toBe(true);expect(screen.queryByRole('heading',{name:'Latest result could not be loaded'})).toBeNull();
});
it('does not present an older answer as the result of a completed run whose output is missing',async()=>{
  setup(async path=>path.endsWith('/workflow')?json({error:{code:'not_found'}},404,binding):path.endsWith('/me')?json(fixture()):path.includes('/runs/')?json({id:r,conversation_id:c,state:'completed',version:2},200,binding):json({conversation:{...conversation,active_run_id:r},messages:[{...answer,run_id:m,text:'Jawaban lama'}],next_after:null},200,binding));
  await screen.findByRole('heading',{name:'Hasil terbaru belum dapat dimuat'});expect(screen.queryByText('Jawaban lama')).toBeNull();expect((screen.getByRole('button',{name:'Kirim pertanyaan'}) as HTMLButtonElement).disabled).toBe(true);
});
it('renders output kinds with human guidance instead of treating insufficient evidence as an outage',async()=>{
  const messages=[{...answer,output_kind:'insufficient_evidence',text:'Tidak ada bukti yang cukup'},{...answer,id:r,sequence:3,output_kind:'source_conflict',text:'Sumber berbeda'},{...answer,id:m,sequence:4,output_kind:'service_unavailable',text:'Layanan gagal'}];
  setup(async path=>path.endsWith('/workflow')?json({error:{code:'not_found'}},404,binding):path.endsWith('/me')?json(fixture()):json({conversation,messages,next_after:null},200,binding));await screen.findByRole('heading',{name:'Informasi belum cukup'});expect(screen.getByRole('heading',{name:'Sumber memberikan informasi berbeda'})).toBeTruthy();expect(screen.getByRole('heading',{name:'Layanan belum tersedia'})).toBeTruthy();expect(screen.getAllByText('Hubungi pengelola pengetahuan perusahaan untuk memastikan informasi yang diperlukan.')).toHaveLength(2);
});
it('reconciles feedback accepted vote and refetches a newer current vote without inference',async()=>{
  const bodies:string[]=[];let voted=false;
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(init.method==='POST'){bodies.push(init.body as string);voted=true;if(bodies.length===1)throw new TypeError('controlled');return json({message_id:c,accepted_rating:'down',result_version:1,current_rating:'up',feedback_version:2},200,binding);}
    return json({conversation,messages:[{...answer,feedback:voted?{rating:'up',reason:null,version:2}:null}],next_after:null},200,binding);
  });const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Tidak membantu'}));await user.type(screen.getByLabelText('Alasan, opsional'),'Perlu penjelasan');await user.click(screen.getByRole('button',{name:'Simpan penilaian'}));await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});await user.click(screen.getByRole('button',{name:'Periksa hasil penyimpanan'}));await screen.findByText('Penilaian tersimpan: Membantu');expect(bodies).toHaveLength(2);expect(bodies[0]).toBe(bodies[1]);expect(JSON.parse(bodies[0]).reason).toBe('Perlu penjelasan');
});
it('does not expose content, citation or rating from a redacted model message',async()=>{
  setup(async path=>path.endsWith('/workflow')?json({error:{code:'not_found'}},404,binding):path.endsWith('/me')?json(fixture()):json({conversation,messages:[{...answer,text:null,redacted:true}],next_after:null},200,binding));await screen.findByText('Isi pesan tidak tersedia karena akses sumber berubah.');expect(screen.queryByRole('button',{name:'Membantu'})).toBeNull();await waitFor(()=>expect(screen.queryByText('Jawaban terbaru')).toBeNull());
});

it('masks stale answer and feedback when returning from a source cannot revalidate history',async()=>{
  let unavailable=false;
  const cited={...answer,sources:[{system:'platform',kind:'document_version',resource_id:versionId,version:'1',page:1,chunk:0}],citations:[{id:'S1',version_id:versionId,page:1,chunk_no:0,text_digest:sourceChunk.text_digest,quote:sourceChunk.text}]};
  setup(async path=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(path.includes('/knowledge/sources/'))return json(sourceValue,200,binding);
    if(unavailable)return json({error:{code:'unavailable',message:'controlled',retryable:true,request_id:m}},503);
    return json({conversation,messages:[cited],next_after:null},200,binding);
  });const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Buka sumber S1'}));await screen.findByText(sourceChunk.text);await user.click(screen.getByRole('button',{name:'Kembali ke posisi sebelumnya'}));await waitFor(()=>expect(document.activeElement).toBe(screen.getByRole('button',{name:'Buka sumber S1'})));await user.click(screen.getByRole('button',{name:'Buka sumber S1'}));await screen.findByText(sourceChunk.text);unavailable=true;await user.click(screen.getByRole('button',{name:'Kembali ke posisi sebelumnya'}));await screen.findByRole('heading',{name:'Akses percakapan belum terverifikasi'});expect(screen.queryByText('Jawaban terbaru')).toBeNull();expect(screen.queryByRole('button',{name:'Membantu'})).toBeNull();
  unavailable=false;await user.click(screen.getByRole('button',{name:'Periksa akses percakapan'}));await screen.findByText('Jawaban terbaru');
});

it('masks a retry snapshot while its authoritative full-page refresh is held',async()=>{
  let phase='initial';let held=0;
  const cited={...answer,sources:[{system:'platform',kind:'document_version',resource_id:versionId,version:'1',page:1,chunk:0}],citations:[{id:'S1',version_id:versionId,page:1,chunk_no:0,text_digest:sourceChunk.text_digest,quote:sourceChunk.text}]};
  setup(async path=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(path.includes('/runs/'))return json({id:m,conversation_id:c,state:'queued',version:1},200,binding);
    if(path.includes('/knowledge/sources/'))return json(sourceValue,200,binding);
    if(phase==='fail')return json({error:{code:'unavailable',message:'controlled',retryable:true,request_id:m}},503);
    if(phase==='retry'){phase='held';return json({conversation:{...conversation,active_run_id:m},messages:[cited],next_after:2},200,binding);}
    if(phase==='held'){held++;return new Promise(()=>{});}
    return json({conversation:{...conversation,active_run_id:m},messages:[cited],next_after:2},200,binding);
  });const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Buka sumber S1'}));await screen.findByText(sourceChunk.text);phase='fail';await user.click(screen.getByRole('button',{name:'Kembali ke posisi sebelumnya'}));await screen.findByRole('heading',{name:'Akses percakapan belum terverifikasi'});await screen.findByRole('heading',{name:'Data belum dapat dimuat'});phase='retry';await user.click(screen.getByRole('button',{name:'Periksa akses percakapan'}));await waitFor(()=>expect(held).toBeGreaterThan(0));await waitFor(()=>expect(screen.queryByText('Jawaban terbaru')).toBeNull());expect(screen.queryByRole('button',{name:'Pesan berikutnya'})).toBeNull();
});
