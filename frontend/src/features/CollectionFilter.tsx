import { useState } from 'react';
import { SelectField } from '../components/Fields';
import { Button } from '../components/Button';
import { decodeCollections } from './knowledge-api';
import { useCopy,useResource } from './workspace-hooks';
import { ResourceState } from './workspace';

export function CollectionFilter({value,onChange,allowAll=true,disabled=false}:{value:string;onChange:(value:string)=>void;allowAll?:boolean;disabled?:boolean}) {
  const c=useCopy();const [cursors,setCursors]=useState<string[]>([]);const after=cursors.at(-1);
  const result=useResource('/api/v1/knowledge/collections?limit=20'+(after?'&after='+after:''),decodeCollections);
  const options=[{value:'',label:allowAll?c('Semua cakupan yang dapat dikelola','All manageable scopes'):c('Pilih cakupan dokumen','Choose document scope')},
    ...(result.data?.tenant_level_allowed?[{value:'tenant',label:c('Dokumen tingkat perusahaan','Company-level documents')}]:[]),
    ...(result.data?.items ?? []).map((item) => ({value:item.collection_id,label:item.display_name ?? item.collection_id}))];
  if (value && !options.some((option) => option.value===value)) options.push({value,label:value==='tenant'?c('Dokumen tingkat perusahaan','Company-level documents'):value});
  return <div><SelectField label={c('Cakupan dokumen','Document scope')} value={value} onChange={(event) => onChange(event.target.value)} disabled={disabled || !result.data} options={options} />
    <ResourceState {...result} retry={result.refresh} />
    {(cursors.length>0 || result.data?.next_after) && <nav className="workspace-actions" aria-label={c('Halaman koleksi','Collection pages')}><Button disabled={disabled || !cursors.length || result.loading} onClick={() => setCursors(cursors.slice(0,-1))}>{c('Koleksi sebelumnya','Previous collections')}</Button><Button disabled={disabled || !result.data?.next_after} onClick={() => {if (result.data?.next_after) setCursors([...cursors,result.data.next_after]);}}>{c('Koleksi berikutnya','Next collections')}</Button></nav>}
  </div>;
}
