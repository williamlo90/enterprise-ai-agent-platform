import { useEffect,useMemo,useRef,useState,useSyncExternalStore } from 'react';
import { ApiFailure } from '../../api/failure';
import { decodeUpload } from '../../api/upload-policy';
import { Button } from '../../components/Button';
import type { UploadMime } from '../../api/upload-policy';
function receiptMime(file:File):UploadMime{const ext=file.name.split('.').at(-1)?.toLowerCase();const mime=ext==='pdf'?'application/pdf':ext==='png'?'image/png':ext==='jpg'||ext==='jpeg'?'image/jpeg':undefined;if(!mime||file.type&&file.type!=='application/octet-stream'&&file.type!==mime)throw new ApiFailure('input');return mime;}
import { identifier,positive } from '../knowledge-api';
import { object } from '../../api/transport';
import { useCopy,useWorkspace } from '../workspace-hooks';
import { useStatusPoll } from '../use-status-poll';
import { PollFeedback } from '../ProcessingStatus';
import { useUnsavedInput } from '../../app/unsaved-context';
import type { Receipt } from './contracts';
interface FileStatus extends Receipt {mime:UploadMime;size_bytes:number;scan_state:'quarantine'|'ready'|'rejected'}
function decode(v:unknown):FileStatus {const r=object(v);if(r.purpose!=='receipt'||!['quarantine','ready','rejected'].includes(r.scan_state as string))throw new ApiFailure('malformed');if(!['application/pdf','image/jpeg','image/png'].includes(r.mime as string))throw new ApiFailure('malformed');return {mime:r.mime as UploadMime,size_bytes:positive(r.size_bytes),id:identifier(r.id),version:positive(r.version),scan_state:r.scan_state as FileStatus['scan_state']};}
const terminal=(f:FileStatus)=>f.scan_state!=='quarantine';
function Inspection({file,onReady}:{file:FileStatus;onReady:(r:Receipt)=>void}) {
 const c=useCopy();const decoder=useMemo(()=>(v:unknown)=>{const r=decode(v);if(r.id!==file.id||r.version<file.version||r.mime!==file.mime||r.size_bytes!==file.size_bytes)throw new ApiFailure('malformed');return r;},[file]);
 const poll=useStatusPoll('/api/v1/files/'+file.id+'/status',decoder,terminal);
 return <><p>{poll.data?.scan_state==='ready'?c('Bukti siap digunakan.','Receipt ready to use.'):poll.data?.scan_state==='rejected'?c('Bukti ditolak. Pilih file lain.','Receipt rejected. Choose another file.'):c('Bukti masih diperiksa dalam karantina.','Receipt is being inspected in quarantine.')}</p><PollFeedback status={poll}/>{poll.data?.scan_state==='ready'&&<Button onClick={()=>onReady(poll.data!)}>{c('Lampirkan bukti ini','Attach this receipt')}</Button>}</>;
}
export function ReceiptUpload({onReady,disabled}:{onReady:(r:Receipt)=>void;disabled:boolean}) {
 const {api,stamp}=useWorkspace();const c=useCopy();const [file,setFile]=useState<File>();const [receipt,setReceipt]=useState<FileStatus>();const [busy,setBusy]=useState(false);const [error,setError]=useState(false);const active=useRef(false);const mounted=useRef(false);const abort=useRef<AbortController|undefined>(undefined);
 useSyncExternalStore(api.intents.subscribe,api.intents.getSnapshot);const pending=api.uploadIntent(stamp);const own=pending&&decodeUpload(pending.material).purpose==='receipt';
 useUnsavedInput(!!file||busy||!!pending);useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;abort.current?.abort();};},[]);
 async function send(reconcile=false){if(active.current)return;active.current=true;setBusy(true);setError(false);let key:string|undefined;
  try {if(pending&&!own||!reconcile&&pending||disabled)throw new ApiFailure('conflict');let intent=pending;
   if(!reconcile){if(!file)throw new ApiFailure('input');abort.current=new AbortController();intent=await api.prepareUpload({file,mime:receiptMime(file),purpose:'receipt'},stamp,undefined,abort.current.signal);key=intent.key;if(!mounted.current){api.discardPrepared(key);return;}}
   if(!intent)throw new ApiFailure('input');const result=await api.dispatchWrite(intent.key,stamp,(v)=>{const r=decode(v),d=decodeUpload(intent.material);if(r.mime!==d.mime||r.size_bytes!==d.size)throw new ApiFailure('malformed');return r;},{reconcile});if(mounted.current){setReceipt(result);setFile(undefined);}
  }catch{if(key&&api.intents.current(key)?.state==='prepared')api.discardPrepared(key);if(mounted.current)setError(true);}finally{active.current=false;if(mounted.current)setBusy(false);}
 }
 return <section className="reimbursement-receipt"><h3>{c('Unggah bukti pengeluaran','Upload an expense receipt')}</h3><p className="field-hint">{c('PDF, JPEG, atau PNG. Maksimum 10 MiB. Nominal tetap deklarasi Anda; isi struk tidak diverifikasi dengan OCR.','PDF, JPEG, or PNG. Maximum 10 MiB. Amounts remain your declarations; receipt contents are not OCR verified.')}</p>
 <label className="field">{c('File bukti','Receipt file')}<input type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" disabled={disabled||busy||!!pending} onChange={e=>{setFile(e.target.files?.[0]);setReceipt(undefined);setError(false);}}/></label>
 <Button disabled={disabled||busy||!!pending||!file} onClick={()=>void send()}>{busy?c('Mengunggah bukti','Uploading receipt'):c('Unggah bukti','Upload receipt')}</Button>
 {pending&&!own&&<p>{c('Selesaikan unggahan dokumen yang masih berjalan terlebih dahulu.','Finish the pending knowledge upload first.')}</p>}
 {error&&<p role="alert">{c('Bukti belum dapat digunakan. Periksa format, ukuran, akses, dan koneksi.','Receipt cannot yet be used. Check format, size, access, and connection.')}</p>}
 {own&&pending.state==='unknown'&&<Button disabled={busy||disabled} onClick={()=>void send(true)}>{c('Periksa hasil unggahan yang sama','Check the same upload result')}</Button>}
 {receipt&&<Inspection file={receipt} onReady={r=>{onReady(r);setReceipt(undefined);}}/>}</section>;
}
