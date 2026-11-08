import { StrictMode } from 'react';
import { act,render,screen,waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter } from 'react-router';
import { afterEach,expect,it } from 'vitest';
import { App } from '../../src/app/App';
import { AuthScreen } from '../../src/app/AuthScreen';
import { AuthController } from '../../src/app/auth-controller';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import type { FetchPort } from '../../src/api/transport';
import { decodeMember,organizationInput } from '../../src/features/organization-api';
import { fixture,json,membership,person,requestId } from './api-fixtures';

const path='/organization/members/'+membership;
const wire={membership_id:membership,principal_id:person,display_name:'Nama akun',local_display_name:null,department:'Finance',job_title:'Manager',groups:['Staff'],organization_version:1,membership_active:true,principal_active:true};
const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const disposers:(()=>void)[]=[];
afterEach(() => {disposers.splice(0).forEach((dispose) => dispose());localStorage.clear();});
function setup(fetch:FetchPort,route=path) {
  const api=new ApiCoordinator(new ApiTransport(fetch),{request:async (_n,_o,work) => work()});const auth=new AuthController(api);
  const router=createMemoryRouter([{path:'*',element:<AuthScreen auth={auth} api={api} />}],{initialEntries:[route]});
  render(<StrictMode><App router={router}/></StrictMode>);disposers.push(() => {router.dispose();auth.dispose();});return {api,router};
}
function identity(role='tenant_admin') {const data=fixture();data.memberships[0].roles=[{role,scope_kind:'tenant',scope_ref:data.active_tenant_id}];return data;}

it('uses tenant-local name override, explicit nullable fields and current version without changing roles',async () => {
  let current={...wire};const bodies:Record<string,unknown>[]=[];
  setup(async (url,init) => {
    if (url.endsWith('/me')) return json(identity());
    if (init.method==='PATCH') {const body=JSON.parse(init.body as string) as Record<string,unknown>;bodies.push(body);current={...current,department:body.department as string,organization_version:2};return json(current,200,binding);}
    return json(current,200,binding);
  });const user=userEvent.setup();const name=await screen.findByRole('textbox',{name:'Nama dalam perusahaan'});
  expect((name as HTMLInputElement).value).toBe('');
  await user.clear(screen.getByRole('textbox',{name:'Departemen'}));await user.type(screen.getByRole('textbox',{name:'Departemen'}),'Legal');
  await user.click(screen.getByRole('button',{name:'Simpan atribut'}));await screen.findByRole('heading',{name:'Atribut berhasil disimpan'});
  expect(bodies).toHaveLength(1);expect(bodies[0]).toMatchObject({display_name:null,department:'Legal',job_title:'Manager',groups:['Staff'],expected_version:1});
  expect(Object.keys(bodies[0]).sort()).toEqual(['department','display_name','expected_version','groups','job_title','key']);
  await waitFor(() => expect((screen.getByRole('textbox',{name:'Departemen'}) as HTMLInputElement).value).toBe('Legal'));
});

it('keeps unknown writes locked and uses the original request on explicit reconciliation',async () => {
  const bodies:string[]=[];let attempt=0;
  setup(async (url,init) => {
    if (url.endsWith('/me')) return json(identity());
    if (init.method==='PATCH') {bodies.push(init.body as string);if (++attempt===1) throw new TypeError('private');return json({...wire,department:'Legal',organization_version:2},200,binding);}
    return json(wire,200,binding);
  });const user=userEvent.setup();await screen.findByRole('textbox',{name:'Departemen'});
  await user.clear(screen.getByRole('textbox',{name:'Departemen'}));await user.type(screen.getByRole('textbox',{name:'Departemen'}),'Legal');await user.click(screen.getByRole('button',{name:'Simpan atribut'}));
  await screen.findByRole('heading',{name:'Hasil penyimpanan belum diketahui'});expect((screen.getByRole('textbox',{name:'Departemen'}) as HTMLInputElement).disabled).toBe(true);
  await user.click(screen.getByRole('button',{name:'Periksa hasil penyimpanan'}));await screen.findByRole('heading',{name:'Atribut berhasil disimpan'});
  expect(bodies).toHaveLength(2);expect(bodies[0]).toBe(bodies[1]);
});

it('keeps stale edits for review and requires confirmation before reloading them',async () => {
  setup(async (url,init) => url.endsWith('/me')?json(identity()):init.method==='PATCH'?json({error:{code:'conflict',message:'private',retryable:false,request_id:requestId}},409):json(wire,200,binding));
  const user=userEvent.setup();await screen.findByRole('textbox',{name:'Departemen'});await user.clear(screen.getByRole('textbox',{name:'Departemen'}));await user.type(screen.getByRole('textbox',{name:'Departemen'}),'Legal');await user.click(screen.getByRole('button',{name:'Simpan atribut'}));
  await screen.findByRole('heading',{name:'Data telah berubah'});expect((screen.getByRole('textbox',{name:'Departemen'}) as HTMLInputElement).value).toBe('Legal');
  await user.click(screen.getByRole('button',{name:'Muat ulang data'}));await screen.findByRole('dialog',{name:'Buang perubahan formulir?'});await user.click(screen.getByRole('button',{name:'Tetap mengedit'}));
  expect((screen.getByRole('textbox',{name:'Departemen'}) as HTMLInputElement).value).toBe('Legal');
});

it('denies employee access without dispatching the organization API',async () => {
  const urls:string[]=[];setup(async (url) => {urls.push(url);return json(identity('employee'));});await screen.findByRole('heading',{name:'Halaman tidak tersedia untuk akses Anda'});
  expect(urls.every((url) => url==='/api/v1/me')).toBe(true);expect(screen.queryByRole('textbox')).toBeNull();
});

it('clears loaded member values immediately when context invalidates',async () => {
  const {api}=setup(async (url) => url.endsWith('/me')?json(identity()):json(wire,200,binding));await screen.findByRole('textbox',{name:'Departemen'});
  act(() => api.invalidate());expect(screen.queryByRole('textbox',{name:'Departemen'})).toBeNull();expect(screen.queryByText('Nama akun')).toBeNull();
});

it('validates organization bounds and rejects malformed private DTOs',() => {
  expect(organizationInput({display_name:' ',department:' Finance ',job_title:'Manager',groups:' Staff\nStaff\n'})).toEqual({body:{display_name:null,department:'Finance',job_title:'Manager',groups:['Staff']},errors:{}});
  expect(organizationInput({display_name:'x'.repeat(201),department:'',job_title:'',groups:Array.from({length:33},(_,i)=>'Group '+i).join('\n')}).errors).toEqual({display_name:'length',groups:'groups'});
  expect(() => decodeMember({...wire,organization_version:1.5})).toThrow();expect(() => decodeMember({...wire,groups:['\u0000']})).toThrow();
});

it('sends literal star and punctuation filters through URLSearchParams without rejecting valid directory input',async () => {
  const queries:string[]=[];
  setup(async (url) => {if (url.endsWith('/me')) return json(identity());queries.push(url);return json({items:[],next_after:null},200,binding);},'/organization');
  const user=userEvent.setup();await screen.findByRole('heading',{name:'Tidak ada anggota yang cocok'});
  await user.type(screen.getByRole('textbox',{name:'Nama atau identitas anggota'}),'Anton*');await user.type(screen.getByRole('textbox',{name:'Grup'}),'Team* & Partners');
  await user.click(screen.getByRole('button',{name:'Terapkan filter'}));
  await waitFor(() => expect(queries.some((url) => {const query=new URL(url,'https://example.test').searchParams;return query.get('q')==='Anton*' && query.get('group')==='Team* & Partners';})).toBe(true));
  expect(screen.queryByRole('heading',{name:'Data belum dapat dimuat'})).toBeNull();
});
