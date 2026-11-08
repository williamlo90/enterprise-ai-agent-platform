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
const id='77777777-7777-4777-8777-777777777777';
const wrong='88888888-8888-4888-8888-888888888888';
const row={id,language:'id',version:1,label:null as string|null,label_source:'empty',active_run_id:null,created_at:'2026-11-06T00:00:00Z',updated_at:'2026-11-06T00:00:00Z'};
const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const disposers:(()=>void)[]=[];
afterEach(()=>{cleanup();disposers.splice(0).forEach(d=>d());localStorage.clear();});
function setup(fetch:FetchPort,path='/chat'){
  const api=new ApiCoordinator(new ApiTransport(fetch),{request:async(_n,_o,work)=>work()});const auth=new AuthController(api);
  const router=createMemoryRouter([{path:'*',element:<AuthScreen auth={auth} api={api}/>}],{initialEntries:[path]});
  render(<StrictMode><App router={router}/></StrictMode>);disposers.push(()=>{router.dispose();auth.dispose();});return {api,router};
}
function history(conversation= row){return {conversation,messages:[],next_after:null};}
it('renders full escaped first-question labels without fetching any conversation history',async()=>{
  const calls:string[]=[];const label='<b>Judul utuh</b> '+ 'x'.repeat(4000);
  setup(async path=>{calls.push(path);return path.endsWith('/me')?json(fixture()):json({items:[{...row,label,label_source:'first_question'}],next_after:null},200,binding);});
  expect((await screen.findByRole('link',{name:label})).textContent).toBe(label);
  expect(document.querySelector('.conversation-list b')).toBeNull();expect(calls.some(p=>p.includes('/messages'))).toBe(false);
});
it('opens a deep-link through bounded history even when it is outside the first list page',async()=>{
  const calls:string[]=[];
  setup(async path=>{calls.push(path);return path.endsWith('/me')?json(fixture()):json(history(),200,binding);},'/chat/'+id);
  await screen.findByRole('heading',{name:'Percakapan baru'});expect(calls.filter(p=>!p.endsWith('/me')).every(p=>p.endsWith('/messages?after=0&limit=20'))).toBe(true);
  expect(screen.getByText('Bahasa percakapan: Indonesia')).toBeTruthy();
});
it('opens the committed conversation without a discard dialog after an explicit nondefault language choice',async()=>{
  const created={...row,language:'en'};
  setup(async(path,init)=>path.endsWith('/me')?json(fixture()):init.method==='POST'?json(created,201,binding):json(path.includes('/messages?')?history(created):{items:[],next_after:null},200,binding));
  const user=userEvent.setup();await user.selectOptions(await screen.findByRole('combobox',{name:'Bahasa percakapan baru'}),'en');await user.click(screen.getByRole('button',{name:'Buat percakapan'}));await screen.findByRole('heading',{name:'Percakapan baru'});
  expect(screen.getByText('Bahasa percakapan: English')).toBeTruthy();expect(screen.queryByRole('dialog')).toBeNull();
});
it('keeps original create language and key through unknown, locale change and page reentry',async()=>{
  const bodies:string[]=[];let posts=0;
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());
    if(init.method==='POST'){bodies.push(init.body as string);if(++posts===1)throw new TypeError('controlled');return json(row,201,binding);}
    return json(path.includes('/messages?')?history():{items:[],next_after:null},200,binding);
  });const user=userEvent.setup();await screen.findByRole('heading',{name:'Belum ada percakapan'});await user.click(screen.getByRole('button',{name:'Buat percakapan'}));await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});
  await user.click(screen.getByRole('link',{name:'Tampilan'}));await user.click(await screen.findByRole('button',{name:'Buang perubahan dan lanjut'}));await user.selectOptions(screen.getByLabelText('Bahasa antarmuka'),'en');
  await user.click(screen.getByRole('link',{name:'Back to workspace'}));const select=await screen.findByRole('combobox',{name:'New conversation language'});
  expect((select as HTMLSelectElement).value).toBe('id');expect((select as HTMLSelectElement).disabled).toBe(true);
  await user.click(screen.getByRole('button',{name:'Check save result'}));await screen.findByRole('heading',{name:'New conversation'});
  expect(bodies).toHaveLength(2);expect(bodies[0]).toBe(bodies[1]);expect(JSON.parse(bodies[0]).language).toBe('id');
});
it('reconciles exact rename and refreshes current metadata instead of treating an old receipt as current',async()=>{
  const bodies:string[]=[];let current={...row,label:'Awal',label_source:'manual'};
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());
    if(init.method==='PATCH'){bodies.push(init.body as string);if(bodies.length===1){current={...current,label:'Nama sesi lain',version:3};throw new TypeError('controlled');}return json({id,label:'  Nama lengkap  ',label_source:'manual',version:2},200,binding);}
    return json(history(current),200,binding);
  },'/chat/'+id);const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Ganti nama percakapan'}));await user.clear(screen.getByLabelText('Nama percakapan'));await user.type(screen.getByLabelText('Nama percakapan'),'  Nama lengkap  ');await user.click(screen.getByRole('button',{name:'Simpan nama'}));await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});
  await user.click(screen.getByRole('button',{name:'Periksa hasil penyimpanan'}));await screen.findByRole('heading',{name:'Nama sesi lain'});expect(bodies).toHaveLength(2);expect(bodies[0]).toBe(bodies[1]);
});
it('keeps a mismatched rename receipt unknown before settlement',async()=>{
  const {api}=setup(async(path,init)=>path.endsWith('/me')?json(fixture()):init.method==='PATCH'?json({id:wrong,label:'Nama',label_source:'manual',version:2},200,binding):json(history(),200,binding),'/chat/'+id);
  const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Ganti nama percakapan'}));await user.type(screen.getByLabelText('Nama percakapan'),'Nama');await user.click(screen.getByRole('button',{name:'Simpan nama'}));await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});
  expect(api.intents.pending('PATCH /api/v1/conversations/'+id+'/name',api.generation)?.state).toBe('unknown');expect((screen.getByLabelText('Nama percakapan') as HTMLTextAreaElement).disabled).toBe(true);
});
it('does not silently rebase a stale CAS rename; user must reload current metadata',async()=>{
  let patches=0;let current=row;
  setup(async(path,init)=>{
    if(path.endsWith('/me'))return json(fixture());
    if(init.method==='PATCH'){patches++;current={...row,version:3,label:'Nama terkini',label_source:'manual'};return json({error:{code:'conflict',message:'controlled',retryable:false,request_id:wrong}},409);}
    return json(history(current),200,binding);
  },'/chat/'+id);const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Ganti nama percakapan'}));await user.type(screen.getByLabelText('Nama percakapan'),'Nama baru');await user.click(screen.getByRole('button',{name:'Simpan nama'}));await screen.findByRole('heading',{name:'Data telah berubah'});expect(patches).toBe(1);
  await user.click(screen.getByRole('button',{name:'Muat ulang percakapan'}));await screen.findByRole('heading',{name:'Nama terkini'});expect(patches).toBe(1);
});
it('focuses a linked validation error and dispatches nothing for a blank name',async()=>{
  let patches=0;setup(async(path,init)=>{if(init.method==='PATCH')patches++;return path.endsWith('/me')?json(fixture()):json(history(),200,binding);},'/chat/'+id);
  const user=userEvent.setup();await user.click(await screen.findByRole('button',{name:'Ganti nama percakapan'}));await user.click(screen.getByRole('button',{name:'Simpan nama'}));const alert=await screen.findByRole('alert');await waitFor(()=>expect(document.activeElement).toBe(alert));expect(screen.getByLabelText('Nama percakapan').getAttribute('aria-describedby')).toBeTruthy();expect(patches).toBe(0);
});
it('shows an error rather than an authorized empty list on service failure',async()=>{
  setup(async path=>path.endsWith('/me')?json(fixture()):json({error:{code:'unavailable',message:'controlled',retryable:true,request_id:wrong}},503));
  await screen.findByRole('heading',{name:'Data belum dapat dimuat'});expect(screen.queryByRole('heading',{name:'Belum ada percakapan'})).toBeNull();
});
