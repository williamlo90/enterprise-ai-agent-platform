import { useState } from 'react';
import { Button } from '../components/Button';
import type { Reader } from './audience-api';
import { useCopy } from './workspace-hooks';

export function ReaderList({readers,onRemove,disabled=false}:{readers:Reader[];onRemove?:(reader:Reader)=>void;disabled?:boolean}) {
  const c=useCopy();const [page,setPage]=useState(0);const current=Math.min(page,Math.max(0,Math.ceil(readers.length/20)-1));
  return <><p>{c('Jumlah pembaca: ','Reader count: ')}{readers.length}</p>
    {readers.length===0?<p>{c('Tidak ada pembaca eksplisit. Ini bukan akses seluruh karyawan.','No explicit readers. This does not grant access to all employees.')}</p>:<ul className="workspace-list">{readers.slice(current*20,current*20+20).map((reader) => <li key={reader.principal_id}><div><p>{reader.display_name ?? reader.principal_id}</p>{reader.display_name && <p className="field-hint">{reader.principal_id}</p>}{!reader.eligible && <p className="field-hint">{c('Tidak aktif; grant tersimpan tetap dapat dihapus. Aktivasi kembali dapat memulihkan akses.','Inactive; the stored grant can still be removed. Reactivation may restore access.')}</p>}</div>{onRemove && <Button disabled={disabled} onClick={() => onRemove(reader)} aria-label={c('Hapus pembaca ','Remove reader ')+(reader.display_name ?? reader.principal_id)}>{c('Hapus','Remove')}</Button>}</li>)}</ul>}
    {readers.length>20 && <nav className="workspace-actions" aria-label={c('Halaman pembaca','Reader pages')}><Button disabled={current===0} onClick={() => setPage(current-1)}>{c('Nama sebelumnya','Previous names')}</Button><span>{current+1}/{Math.ceil(readers.length/20)}</span><Button disabled={(current+1)*20>=readers.length} onClick={() => setPage(current+1)}>{c('Nama berikutnya','Next names')}</Button></nav>}
  </>;
}
