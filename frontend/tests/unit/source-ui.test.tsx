import { StrictMode } from 'react';
import { act,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter } from 'react-router';
import { afterEach,expect,it,vi } from 'vitest';
import { App } from '../../src/app/App';
import { AuthScreen } from '../../src/app/AuthScreen';
import { AuthController } from '../../src/app/auth-controller';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { fixture,json,requestId } from './api-fixtures';
import { sourceHit,sourceValue,versionId } from './source-fixtures';

const binding={'X-EAI-Context-Binding':'a'.repeat(64)};const disposers:(()=>void)[]=[];
afterEach(() => {disposers.splice(0).forEach((dispose) => dispose());localStorage.clear();sessionStorage.clear();});
function setup(fetch:FetchPort,path='/search') {
  let tail:Promise<unknown>=Promise.resolve();const api=new ApiCoordinator(new ApiTransport(fetch),{request:async (_name,_options,work) => {const next=tail.then(work);tail=next.catch(() => {});return next;}});const auth=new AuthController(api);
  const router=createMemoryRouter([{path:'*',element:<AuthScreen auth={auth} api={api} />}],{initialEntries:[path]});render(<StrictMode><App router={router} /></StrictMode>);disposers.push(() => {router.dispose();auth.dispose();api.dispose();});return {api,router};
}
function normal(url:string) {if (url.endsWith('/me')) return json(fixture());return json(url.includes('/search?')?{items:[sourceHit],search_mode:'lexical_simple_v1'}:sourceValue,200,binding);}
async function search() {const user=userEvent.setup();await user.type(await screen.findByRole('textbox',{name:'Kata pencarian'}),'biaya & perjalanan');await user.click(screen.getByRole('button',{name:'Cari'}));return user;}

it('submits search only on explicit action, keeps q out of navigation/storage, and uses no manager directory API',async () => {
  const urls:string[]=[];const {router}=setup(async (url) => {urls.push(url);return normal(url);});const user=userEvent.setup();await user.type(await screen.findByRole('textbox',{name:'Kata pencarian'}),'biaya');expect(urls.every((url) => url.endsWith('/me'))).toBe(true);
  await user.selectOptions(screen.getByRole('combobox',{name:'Bahasa pencarian'}),'en');await user.click(screen.getByRole('button',{name:'Cari'}));await screen.findByRole('button',{name:sourceHit.title});
  expect(urls.some((url) => url.includes('language=en'))).toBe(true);expect(urls.every((url) => url.endsWith('/me') || url.includes('/knowledge/search?'))).toBe(true);expect(router.state.location.search).toBe('');expect(localStorage.length).toBe(0);expect(sessionStorage.length).toBe(0);
});
it('freshly authorizes exact source highlight on every open and restores the trigger only after a fresh context check',async () => {
  vi.spyOn(window,'scrollTo').mockImplementation(() => {});let reads=0;setup(async (url) => {if (url.includes('/sources/')) reads++;return normal(url);});const user=await search();const trigger=await screen.findByRole('button',{name:sourceHit.title});await user.click(trigger);
  await waitFor(() => expect(document.querySelector('mark')?.textContent).toBe(sourceHit.text));expect(reads).toBe(1);await user.click(screen.getByRole('button',{name:'Kembali ke posisi sebelumnya'}));await waitFor(() => expect(document.activeElement).toBe(trigger));expect(document.querySelector('mark')).toBeNull();
  await user.click(trigger);await waitFor(() => expect(reads).toBe(2));
});
it('purges prior source and search excerpts on current permission denial without falling back to cached text',async () => {
  let denied=false;setup(async (url) => denied && url.includes('/sources/')?json({},404,binding):normal(url));const user=await search();await user.click(await screen.findByRole('button',{name:sourceHit.title}));await screen.findByRole('button',{name:'Periksa akses terbaru'});denied=true;await user.click(screen.getByRole('button',{name:'Periksa akses terbaru'}));
  await screen.findByRole('heading',{name:'Sumber tidak tersedia'});expect(screen.queryByRole('button',{name:sourceHit.title})).toBeNull();expect(screen.queryByText(sourceHit.text)).toBeNull();expect(document.querySelector('mark')).toBeNull();
});
it('distinguishes authorized empty from unavailable and honors Retry-After before manual retry',async () => {
  let empty=false;let reads=0;setup(async (url) => {if (url.includes('/search?')) {reads++;return empty?json({items:[],search_mode:'lexical_simple_v1'},200,binding):json({error:{code:'rate_limited',message:'private',retryable:true,request_id:requestId}},429,{...binding,'Retry-After':'30'});}return normal(url);});const user=await search();await screen.findByRole('heading',{name:'Data belum dapat dimuat'});expect((screen.getByRole('button',{name:'Periksa kembali'}) as HTMLButtonElement).disabled).toBe(true);await user.click(screen.getByRole('button',{name:'Cari'}));expect(reads).toBe(1);empty=true;
});
it('shows a successful authorized empty search without treating it as denial',async () => {
  setup(async (url) => url.includes('/search?')?json({items:[],search_mode:'lexical_simple_v1'},200,binding):normal(url));await search();await screen.findByRole('heading',{name:'Tidak ada sumber yang cocok'});expect(screen.queryByRole('alert')).toBeNull();
});
it('discards a cancelled old query response when a newer query is submitted',async () => {
  let release:(response:Response)=>void=()=>{};let entered=false;setup(async (url) => {
    if (url.includes('/search?') && new URL(url,'https://example.test').searchParams.get('q')==='slow') {entered=true;return new Promise<Response>((resolve) => {release=resolve;});}
    return normal(url);
  });const user=userEvent.setup();const field=await screen.findByRole('textbox',{name:'Kata pencarian'});await user.type(field,'slow');await user.click(screen.getByRole('button',{name:'Cari'}));await waitFor(() => expect(entered).toBe(true));await user.clear(field);await user.type(field,'current');await user.click(screen.getByRole('button',{name:'Cari'}));
  release(json({items:[{...sourceHit,title:'STALE RESPONSE'}],search_mode:'lexical_simple_v1'},200,binding));await screen.findByRole('button',{name:sourceHit.title});expect(screen.queryByText('STALE RESPONSE')).toBeNull();expect(screen.getByRole('button',{name:'Keluar'})).toBeTruthy();
});
it('does not restore focus or position when the context changes during mobile return',async () => {
  const scroll=vi.spyOn(window,'scrollTo').mockImplementation(() => {});let changed=false;setup(async (url) => url.endsWith('/me') && changed?json({...fixture(),context_binding:'e'.repeat(64)}):normal(url));const user=await search();await user.click(await screen.findByRole('button',{name:sourceHit.title}));await screen.findByRole('button',{name:'Periksa akses terbaru'});changed=true;
  await user.click(screen.getByRole('button',{name:'Kembali ke posisi sebelumnya'}));await screen.findByRole('heading',{name:'Konteks akses perlu diperiksa ulang'});expect(scroll).not.toHaveBeenCalled();expect(document.querySelector('mark')).toBeNull();
});
it('loads superseded deep links directly without discovery and clears them on context invalidation',async () => {
  const urls:string[]=[];const {api}=setup(async (url) => {urls.push(url);return url.endsWith('/me')?normal(url):json({...sourceValue,publication_state:'superseded'},200,binding);},'/sources/'+versionId);
  await screen.findByText(/Versi lama — telah digantikan/);expect(urls.every((url) => url.endsWith('/me') || url.endsWith('/sources/'+versionId+'/content'))).toBe(true);act(() => api.invalidate());expect(screen.queryByText(sourceHit.text)).toBeNull();
});
