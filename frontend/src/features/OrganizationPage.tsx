import { useId, useRef, useState } from 'react';
import { Link } from 'react-router';
import { TextField } from '../components/Fields';
import { Button } from '../components/Button';
import { StatusMessage } from '../components/StatusMessage';
import { Modal } from '../components/Modal';
import { useUnsavedInput } from '../app/unsaved-context';
import { decodeMember,decodeMembers,memberForm,organizationInput } from './organization-api';
import type { OrganizationForm,OrganizationMember } from './organization-api';
import { ResourceState,WriteState } from './workspace';
import { useCopy,useResource,useWrite } from './workspace-hooks';

export function OrganizationDirectory() {
  const c=useCopy();const [filters,setFilters]=useState({q:'',department:'',job_title:'',group:''});
  const [query,setQuery]=useState('');const [cursors,setCursors]=useState<string[]>([]);
  const params=new URLSearchParams(query);const after=cursors.at(-1);if (after) params.set('after',after);params.set('limit','20');
  const result=useResource('/api/v1/organization/members?'+params.toString(),decodeMembers);
  return <><div className="page-heading"><h1>{c('Organisasi','Organization')}</h1><p>{c('Atribut anggota perusahaan untuk pencarian dan pemilihan pembaca.','Company member attributes for search and reader selection.')}</p></div>
    <form className="workspace-filters" onSubmit={(event) => {event.preventDefault();const next=new URLSearchParams();for (const [name,value] of Object.entries(filters)) if (value.trim()) next.set(name,value.trim());setQuery(next.toString());setCursors([]);}}>
      {(['q','department','job_title','group'] as const).map((field) => <TextField key={field} label={field==='q'?c('Nama atau identitas anggota','Member name or identifier'):field==='department'?c('Departemen','Department'):field==='job_title'?c('Jabatan','Job title'):c('Grup','Group')} value={filters[field]} maxLength={field==='q'?200:100} onChange={(event) => setFilters({...filters,[field]:event.target.value})} />)}
      <Button type="submit">{c('Terapkan filter','Apply filters')}</Button>
    </form>
    <ResourceState {...result} retry={result.refresh} />
    {result.data && <>{result.data.items.length===0?<StatusMessage kind="empty" title={c('Tidak ada anggota yang cocok','No matching members')} />:
      <ul className="workspace-list">{result.data.items.map((member) => <li key={member.membership_id}><div><Link to={'/organization/members/'+member.membership_id}>{member.display_name ?? member.principal_id}</Link><p className="field-hint">{member.department ?? c('Departemen belum dicatat','Department not recorded')} · {member.job_title ?? c('Jabatan belum dicatat','Job title not recorded')}</p>{(!member.membership_active || !member.principal_active) && <p className="field-hint">{c('Tidak aktif','Inactive')}</p>}</div><Link className="button" to={'/organization/members/'+member.membership_id}>{c('Periksa anggota','View member')}</Link></li>)}</ul>}
      <nav className="workspace-actions" aria-label={c('Halaman anggota','Member pages')}><Button disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0,-1))}>{c('Sebelumnya','Previous')}</Button><Button disabled={!result.data.next_after} onClick={() => {if (result.data?.next_after) setCursors([...cursors,result.data.next_after]);}}>{c('Berikutnya','Next')}</Button></nav></>}
  </>;
}

export function OrganizationDetail({membershipId}:{membershipId:string}) {
  const c=useCopy();const result=useResource('/api/v1/organization/members/'+membershipId,decodeMember);
  const [edition,setEdition]=useState(0);
  const [saved,setSaved]=useState(false);
  return <><Link to="/organization">{c('Kembali ke organisasi','Back to organization')}</Link><div className="page-heading"><h1>{c('Atribut anggota','Member attributes')}</h1><p>{c('Perubahan ini berlaku untuk perusahaan aktif. Peran dan akses dokumen tidak berubah.','Changes apply to the active company. Application roles and document access remain unchanged.')}</p></div>
    <ResourceState {...result} retry={result.refresh} />
    {saved && <StatusMessage kind="success" title={c('Atribut berhasil disimpan','Attributes saved')}><p>{c('Data terbaru dimuat kembali untuk memastikan keadaan saat ini.','Current data is reloaded to confirm its latest state.')}</p></StatusMessage>}
    {result.data && <OrganizationEditor key={membershipId+':'+edition} member={result.data} reload={(committed=false) => {setSaved(committed);setEdition((value) => value+1);result.refresh();}} />}
  </>;
}

function OrganizationEditor({member,reload}:{member:OrganizationMember;reload:(committed?:boolean)=>void}) {
  const c=useCopy();const id=useId();const [initial]=useState(() => memberForm(member));const [form,setForm]=useState(initial);
  const [errors,setErrors]=useState<Partial<Record<keyof OrganizationForm,string>>>({});const summary=useRef<HTMLDivElement>(null);const [saved,setSaved]=useState(false);
  const [confirmReload,setConfirmReload]=useState(false);
  const write=useWrite('PATCH','/api/v1/organization/members/'+member.membership_id,decodeMember);
  const dirty=JSON.stringify(form)!==JSON.stringify(initial);useUnsavedInput(dirty && !saved);
  async function save(reconcile=false) {
    setSaved(false);const parsed=organizationInput(form);
    if (!reconcile && Object.keys(parsed.errors).length) {setErrors(Object.fromEntries(Object.entries(parsed.errors).map(([field]) => [field,c('Periksa panjang dan format isian. Grup paling banyak 32, satu nama per baris.','Check input length and format. Use at most 32 groups, one name per line.')])));queueMicrotask(() => summary.current?.focus());return;}
    setErrors({});const receipt=await write.execute(reconcile?undefined:{...parsed.body,expected_version:member.organization_version});
    if (receipt) {setSaved(true);reload(true);}
  }
  const locked=write.busy || write.unknown || saved || write.error?.kind==='conflict';
  return <section className="settings-section"><div className="settings-description"><h2>{member.display_name ?? member.principal_id}</h2><p>{c('Identitas anggota','Member identifier')}: {member.principal_id}</p><p>{c('Status','Status')}: {member.membership_active && member.principal_active?c('Aktif','Active'):c('Tidak aktif','Inactive')}</p><p>{c('Nama perusahaan dapat dikosongkan untuk menggunakan nama akun yang tersedia. Atribut ini bukan pemberian akses.','Clear the company name override to use the available account name. These attributes do not grant access.')}</p></div>
    <form className="settings-fields" onSubmit={(event) => {event.preventDefault();void save();}}>
      {Object.keys(errors).length>0 && <div ref={summary} tabIndex={-1} role="alert"><h2>{c('Periksa isian berikut','Check the following fields')}</h2><ul>{Object.entries(errors).map(([field,message]) => <li key={field}><a href={'#'+id+'-'+field}>{message}</a></li>)}</ul></div>}
      {(['display_name','department','job_title'] as const).map((field) => <TextField key={field} id={id+'-'+field} label={field==='display_name'?c('Nama dalam perusahaan','Company display name'):field==='department'?c('Departemen','Department'):c('Jabatan','Job title')} value={form[field]} disabled={locked} error={errors[field]} onChange={(event) => setForm({...form,[field]:event.target.value})} />)}
      <div className="field"><label htmlFor={id+'-groups'}>{c('Grup','Groups')}</label><textarea id={id+'-groups'} rows={4} value={form.groups} disabled={locked} aria-invalid={!!errors.groups} aria-describedby={id+'-groups-hint'+(errors.groups?' '+id+'-groups-error':'')} onChange={(event) => setForm({...form,groups:event.target.value})} /><p className="field-hint" id={id+'-groups-hint'}>{c('Satu nama grup per baris. Kosongkan untuk menghapus semua grup.','One group name per line. Leave empty to clear all groups.')}</p>{errors.groups && <p className="field-error" id={id+'-groups-error'}>{errors.groups}</p>}</div>
      <div className="workspace-actions"><Button type="submit" variant="primary" disabled={locked || !dirty}>{write.busy?c('Menyimpan','Saving'):c('Simpan atribut','Save attributes')}</Button><Button disabled={write.busy || write.unknown} onClick={() => {if (dirty) setConfirmReload(true);else reload();}}>{c('Muat ulang data','Reload data')}</Button></div>
      <WriteState {...write} reconcile={() => {void save(true);}} />
      <Modal open={confirmReload} title={c('Buang perubahan formulir?','Discard form changes?')} description={c('Isian yang belum disimpan akan diganti dengan data terbaru.','Unsaved fields will be replaced by current data.')} closeLabel={c('Tetap mengedit','Keep editing')} onClose={() => setConfirmReload(false)}><Button variant="danger" onClick={() => reload()}>{c('Buang dan muat ulang','Discard and reload')}</Button></Modal>
    </form>
  </section>;
}
