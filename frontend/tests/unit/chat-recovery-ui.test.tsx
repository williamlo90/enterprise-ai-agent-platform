import {StrictMode} from 'react';
import {cleanup,render,screen,waitFor,act,fireEvent} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {createMemoryRouter} from 'react-router';
import {afterEach,expect,it,vi} from 'vitest';
import {App} from '../../src/app/App';
import {AuthScreen} from '../../src/app/AuthScreen';
import {AuthController} from '../../src/app/auth-controller';
import {ApiCoordinator} from '../../src/app/api-coordinator';
import {ApiTransport} from '../../src/api/transport';
import type {FetchPort} from '../../src/api/transport';
import {fixture,json} from './api-fixtures';
import {sourceValue,sourceChunk,versionId} from './source-fixtures';
import {diagnostic,rid} from './chat-recovery-fixtures';
const cid='77777777-7777-4777-8777-777777777777';const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const conversation={id:cid,language:'id',version:3,label:'Percakapan',label_source:'manual',active_run_id:rid,created_at:'2026-11-06T00:00:00Z',updated_at:'2026-11-06T00:00:00Z'};
const message={id:cid,sequence:2,author_kind:'model',run_id:rid,output_kind:'answer',text:'Jawaban tersimpan',redacted:false,sources:[],citations:[],feedback:null};
const disposers:(()=>void)[]=[];afterEach(()=>{cleanup();disposers.splice(0).forEach(d=>d());vi.useRealTimers();localStorage.clear();});
function setup(fetch:FetchPort){const api=new ApiCoordinator(new ApiTransport(fetch),{request:async(_n,_o,work)=>work()});const auth=new AuthController(api);const router=createMemoryRouter([{path:'*',element:<AuthScreen api={api} auth={auth}/>}],{initialEntries:['/chat/'+cid]});render(<StrictMode><App router={router}/></StrictMode>);disposers.push(()=>{router.dispose();auth.dispose();});return api;}
it('confirms a no-key CAS close and discovers server state before enabling send',async()=>{
  const bodies:string[]=[];let closed=false;
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(init.method==='POST'){bodies.push(init.body as string);closed=true;return json({...diagnostic,state:'failed',version:4,recovery_available:false,recovery:'closed_without_replay',recovered_at:'2026-11-06T00:00:00Z'},200,binding);}
    if(path.endsWith('/diagnostics'))return json(closed?{...diagnostic,state:'failed',version:4,recovery_available:false,recovered_at:'2026-11-06T00:00:00Z'}:diagnostic,200,binding);
    if(path.includes('/runs/'))return json({id:rid,conversation_id:cid,state:closed?'failed':'held',version:closed?4:3},200,binding);
    return json({conversation:{...conversation,active_run_id:closed?null:rid},messages:[],next_after:null},200,binding);
  });const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Lihat detail proses'}));await screen.findByText(/Pemakaian belum pasti untuk/);await user.click(screen.getByRole('button',{name:'Tutup proses tertahan'}));expect(bodies).toHaveLength(0);await user.click(screen.getByRole('button',{name:'Ya, tutup tanpa menjalankan ulang'}));await waitFor(()=>expect((screen.getByRole('button',{name:'Kirim pertanyaan'}) as HTMLButtonElement).disabled).toBe(false));expect(bodies).toEqual([JSON.stringify({expected_version:3})]);
});
it('lost recover response reconciles by GET without a second mutation or inference',async()=>{
  let posts=0,closed=false;
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(init.method==='POST'){posts++;closed=true;throw new TypeError('controlled');}
    if(path.endsWith('/diagnostics'))return json(closed?{...diagnostic,state:'failed',version:4,recovered_at:'2026-11-06T00:00:00Z',recovery_available:false}:diagnostic,200,binding);
    if(path.includes('/runs/'))return json({id:rid,conversation_id:cid,state:closed?'failed':'held',version:closed?4:3},200,binding);
    return json({conversation:{...conversation,active_run_id:closed?null:rid},messages:[],next_after:null},200,binding);
  });const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Lihat detail proses'}));await user.click(await screen.findByRole('button',{name:'Tutup proses tertahan'}));await user.click(screen.getByRole('button',{name:'Ya, tutup tanpa menjalankan ulang'}));await screen.findByRole('heading',{name:'Hasil penutupan belum diketahui'});await user.click(screen.getByRole('button',{name:'Periksa status penutupan'}));await waitFor(()=>expect(screen.queryByRole('heading',{name:'Hasil penutupan belum diketahui'})).toBeNull());expect(posts).toBe(1);await waitFor(()=>expect((screen.getByRole('button',{name:'Kirim pertanyaan'}) as HTMLButtonElement).disabled).toBe(false));
});
it('periodic visible history rechecks old messages and purges feedback and the open source',async()=>{
  vi.useFakeTimers();let revoked=false,historyReads=0;
  const cited={...message,sources:[{system:'platform',kind:'document_version',resource_id:versionId,version:'1',page:1,chunk:0}],citations:[{id:'S1',version_id:versionId,page:1,chunk_no:0,text_digest:sourceChunk.text_digest,quote:sourceChunk.text}]};
  setup(async path=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(path.includes('/knowledge/sources/'))return revoked?json({error:{code:'not_found',message:'unavailable',retryable:false,request_id:cid}},404):json(sourceValue,200,binding);
    historyReads++;return json({conversation:{...conversation,active_run_id:null},messages:[revoked?{...message,text:null,redacted:true}:cited],next_after:null},200,binding);
  });await act(()=>vi.advanceTimersByTimeAsync(0));fireEvent.click(screen.getByRole('button',{name:'Buka sumber S1'}));await act(()=>vi.advanceTimersByTimeAsync(0));expect(screen.getByText(sourceChunk.text)).toBeTruthy();revoked=true;await act(()=>vi.advanceTimersByTimeAsync(10001));expect(historyReads).toBeGreaterThan(1);expect(screen.queryByText('Jawaban tersimpan')).toBeNull();expect(screen.queryByRole('button',{name:'Membantu'})).toBeNull();expect(screen.queryByText(sourceChunk.text)).toBeNull();
});
it('preserves feedback and rename drafts across a slow visible-page refresh',async()=>{
  vi.useFakeTimers();let held=false;const resolve:((value:Response)=>void)[]=[];
  setup(async path=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(held)return new Promise<Response>(r=>resolve.push(r));
    return json({conversation:{...conversation,active_run_id:null},messages:[message],next_after:null},200,binding);
  });await act(()=>vi.advanceTimersByTimeAsync(0));fireEvent.click(screen.getByRole('button',{name:'Tidak membantu'}));fireEvent.change(screen.getByLabelText('Alasan, opsional'),{target:{value:'Alasan belum disimpan'}});fireEvent.click(screen.getByRole('button',{name:'Ganti nama percakapan'}));fireEvent.change(screen.getByLabelText('Nama percakapan'),{target:{value:'Percakapan baru'}});held=true;await act(()=>vi.advanceTimersByTimeAsync(12001));expect(screen.queryByLabelText('Alasan, opsional')).toBeNull();held=false;await act(async()=>{resolve[0](json({conversation:{...conversation,active_run_id:null},messages:[message],next_after:null},200,binding));await vi.advanceTimersByTimeAsync(0);});expect((screen.getByLabelText('Alasan, opsional') as HTMLTextAreaElement).value).toBe('Alasan belum disimpan');expect((screen.getByLabelText('Nama percakapan') as HTMLTextAreaElement).value).toBe('Percakapan baru');
});
it('preserves the original rename CAS version when periodic refresh observes another rename',async()=>{
  vi.useFakeTimers();let version=3;const patches:Record<string,unknown>[]=[];
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(init.method==='PATCH'){patches.push(JSON.parse(init.body as string));return json({error:{code:'conflict',message:'controlled',retryable:false,request_id:cid}},409);}
    return json({conversation:{...conversation,version,label:version===3?'Percakapan':'Nama sesi lain',active_run_id:null},messages:[],next_after:null},200,binding);
  });await act(()=>vi.advanceTimersByTimeAsync(0));fireEvent.click(screen.getByRole('button',{name:'Ganti nama percakapan'}));fireEvent.change(screen.getByLabelText('Nama percakapan'),{target:{value:'Draft saya'}});version=4;await act(()=>vi.advanceTimersByTimeAsync(10001));expect((screen.getByLabelText('Nama percakapan') as HTMLTextAreaElement).value).toBe('Draft saya');fireEvent.click(screen.getByRole('button',{name:'Simpan nama'}));await act(()=>vi.advanceTimersByTimeAsync(0));expect(patches).toHaveLength(1);expect(patches[0].expected_version).toBe(3);expect(screen.getByRole('heading',{name:'Data telah berubah'})).toBeTruthy();
});
it('masks a failed periodic read and respects Retry-After before manual retry',async()=>{
  vi.useFakeTimers();let limited=false,reads=0;
  setup(async path=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);reads++;
    if(limited)return json({error:{code:'unavailable',message:'controlled',retryable:true,request_id:cid}},429,{'Retry-After':'5'});
    return json({conversation:{...conversation,active_run_id:null},messages:[message],next_after:null},200,binding);
  });await act(()=>vi.advanceTimersByTimeAsync(0));limited=true;await act(()=>vi.advanceTimersByTimeAsync(10001));expect(screen.queryByText('Jawaban tersimpan')).toBeNull();const before=reads;expect((screen.getByRole('button',{name:'Periksa kembali'}) as HTMLButtonElement).disabled).toBe(true);fireEvent.click(screen.getByRole('button',{name:'Periksa kembali'}));await act(()=>vi.advanceTimersByTimeAsync(4998));expect(reads).toBe(before);await act(()=>vi.advanceTimersByTimeAsync(2));expect((screen.getByRole('button',{name:'Periksa kembali'}) as HTMLButtonElement).disabled).toBe(false);limited=false;fireEvent.click(screen.getByRole('button',{name:'Periksa kembali'}));await act(()=>vi.advanceTimersByTimeAsync(0));expect(screen.getByText('Jawaban tersimpan')).toBeTruthy();
});
it('discards a late history buffer after context invalidation',async()=>{
  vi.useFakeTimers();let held=false;const pending:((value:Response)=>void)[]=[];
  const api=setup(async path=>{
    if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    if(held)return new Promise<Response>(resolve=>pending.push(resolve));
    return json({conversation:{...conversation,active_run_id:null},messages:[message],next_after:null},200,binding);
  });await act(()=>vi.advanceTimersByTimeAsync(0));held=true;await act(()=>vi.advanceTimersByTimeAsync(10001));act(()=>api.invalidate());expect(screen.queryByText('Jawaban tersimpan')).toBeNull();await act(async()=>{pending[0](json({conversation:{...conversation,active_run_id:null},messages:[{...message,text:'Late private buffer'}],next_after:null},200,binding));await vi.advanceTimersByTimeAsync(0);});expect(screen.queryByText('Late private buffer')).toBeNull();
});

it('fresh reconnect with no active pointer discovers a completed answer beyond20 messages without POST',async()=>{
  const calls:string[]=[];const first=Array.from({length:20},(_,i)=>({...message,id:'10000000-0000-4000-8000-'+String(i+1).padStart(12,'0'),sequence:i+1,author_kind:i%2?'model':'human',output_kind:i%2?'answer':null,text:'Pesan lama '+i}));
  setup(async(path,init)=>{
    calls.push((init.method??'GET')+' '+path);if(path.endsWith('/me'))return json(fixture());if(path.endsWith('/workflow'))return json({error:{code:'not_found'}},404,binding);
    return json({conversation:{...conversation,active_run_id:null},messages:path.includes('after=20')?[{...message,id:rid,sequence:21,author_kind:'human',output_kind:null,text:'Pertanyaan terakhir'},{...message,sequence:22,text:'Jawaban terakhir tersimpan'}]:first,next_after:path.includes('after=20')?null:20},200,binding);
  });await screen.findByText('Jawaban terakhir tersimpan');expect(calls.some(p=>p.includes('after=20'))).toBe(true);expect(calls.some(p=>p.startsWith('POST'))).toBe(false);expect(screen.queryByText('Pesan lama 0')).toBeNull();
});
