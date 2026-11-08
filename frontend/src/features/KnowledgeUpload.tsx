import { useEffect,useId,useMemo,useRef,useState,useSyncExternalStore } from 'react';
import { Link } from 'react-router';
import { ApiFailure } from '../api/failure';
import { decodeUpload } from '../api/upload-policy';
import { useUnsavedInput } from '../app/unsaved-context';
import { Button } from '../components/Button';
import { StatusMessage } from '../components/StatusMessage';
import { CollectionFilter } from './CollectionFilter';
import { fileMime,fileDecoder } from './upload-api';
import type { KnowledgeFile } from './upload-api';
import { useCopy,useWorkspace } from './workspace-hooks';
import { useStatusPoll } from './use-status-poll';
import { PollFeedback } from './ProcessingStatus';
import { KnowledgeAdmission } from './KnowledgeAdmission';
import type { KnowledgeDocument } from './knowledge-api';
import { knowledgePending } from './knowledge-operations';
import { PendingKnowledgeOperations } from './PendingKnowledgeOperations';

const fileTerminal=(file:KnowledgeFile) => file.scan_state!=='quarantine';
function FileInspection({receipt,collectionId,replacement,onAdmitted,admitted}:{receipt:KnowledgeFile;collectionId:string|null;replacement?:KnowledgeDocument;onAdmitted:()=>void;admitted:boolean}) {
  const c=useCopy();const decode=useMemo(() => fileDecoder({id:receipt.id,mime:receipt.mime,size:receipt.size_bytes,version:receipt.version}),[receipt]);
  const status=useStatusPoll('/api/v1/files/'+receipt.id+'/status',decode,fileTerminal);
  return <section aria-label={c('Hasil pemeriksaan file','File inspection result')}>
    <h2>{c('Pemeriksaan file','File inspection')}</h2>
    <p className="field-hint">{c('ID file: ','File ID: ')}{receipt.id}</p>
    {status.data && !admitted && <StatusMessage kind={status.data.scan_state==='rejected'?'error':status.data.scan_state==='ready'?'success':'loading'}
      title={status.data.scan_state==='ready'?c('File siap untuk pembuatan draft','File ready for draft creation'):status.data.scan_state==='rejected'?c('File ditolak','File rejected'):c('File dalam karantina','File in quarantine')}>
      <p>{status.data.scan_state==='ready'?c('File telah lolos pemeriksaan. File ini belum menjadi dokumen dan belum diterbitkan.','The file passed inspection. It is not yet a document and has not been published.'):status.data.scan_state==='rejected'?c('File tidak dapat digunakan. Periksa format dan isi file asal sebelum mengunggah file baru.','This file cannot be used. Check the original format and contents before uploading a new file.'):c('Server masih memeriksa file. Pembuatan draft belum tersedia untuk file ini.','The server is still inspecting the file. This file is not ready for draft creation.')}</p>
    </StatusMessage>}
    <PollFeedback status={status} />
    {status.data?.scan_state==='ready' && <KnowledgeAdmission file={status.data} collectionId={collectionId} replacement={replacement} onAdmitted={onAdmitted} />}
    <p className="field-hint">{c('Status file ini tersedia selama halaman terbuka. Memuat ulang halaman tidak memulihkan unggahan ini.','This file status is available while the page is open. Reloading the page does not restore this upload.')}</p>
  </section>;
}

export function KnowledgeUpload({replacement,onAdmitted}:{replacement?:KnowledgeDocument;onAdmitted?:()=>void}={}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const inputId=useId();
  const [collection,setCollection]=useState(replacement?replacement.collection_id ?? 'tenant':'');const [file,setFile]=useState<File>();const [inputRevision,setInputRevision]=useState(0);
  const [busy,setBusy]=useState(false);const [phase,setPhase]=useState<'hashing'|'sending'>('hashing');const [error,setError]=useState<ApiFailure>();const [receipt,setReceipt]=useState<KnowledgeFile>();
  const [admitted,setAdmitted]=useState(false);const Heading=replacement?'h3':'h1';
  const hasAdmission=knowledgePending(api,stamp).some((row) => row.entry.context.documentId===(replacement?.document_id ?? null));
  const mounted=useRef(false);const active=useRef(false);const hashing=useRef<AbortController | undefined>(undefined);const prepared=useRef<string | undefined>(undefined);
  useSyncExternalStore(api.intents.subscribe,api.intents.getSnapshot);
  const intent=api.uploadIntent(stamp);const descriptor=intent?decodeUpload(intent.material):undefined;
  const blocked=busy || !!intent || hasAdmission;const unknown=intent?.state==='unknown';
  useUnsavedInput(!!file || blocked || !!receipt && !admitted);
  useEffect(() => {
    mounted.current=true;
    return () => {
      mounted.current=false;hashing.current?.abort();
      const key=prepared.current;if (key && api.intents.current(key)?.state==='prepared') api.discardPrepared(key);
    };
  },[api]);
  function clearFile() {setFile(undefined);setInputRevision((value) => value+1);}
  async function submit(reconcile=false) {
    if (active.current) return;
    active.current=true;setBusy(true);setError(undefined);
    let ownKey:string|undefined;
    try {
      const pending=api.uploadIntent(stamp);
      if (pending && decodeUpload(pending.material).purpose==='receipt') throw new ApiFailure('conflict');
      if (reconcile ? pending?.state!=='unknown' : !!pending || hasAdmission || !file || !collection) throw new ApiFailure('conflict');
      let upload=pending;
      if (!reconcile) {
        const controller=new AbortController();hashing.current=controller;setPhase('hashing');
        upload=await api.prepareUpload({file:file!,mime:fileMime(file!),collectionId:collection==='tenant'?null:collection},stamp,undefined,controller.signal);
        ownKey=upload.key;prepared.current=upload.key;
        if (!mounted.current || controller.signal.aborted) {api.discardPrepared(upload.key);return;}
        clearFile();
      }
      if (!upload) throw new ApiFailure('input');
      const material=decodeUpload(upload.material);setPhase('sending');
      // Dispatch belongs to the intent, not the view. Unmount cannot prove that
      // the server cancelled it; reentry subscribes to its pending/unknown state.
      const result=await api.dispatchWrite(upload.key,stamp,fileDecoder({mime:material.mime,size:material.size}),{reconcile});
      if (mounted.current) setReceipt(result);
    } catch (failure) {
      if (ownKey && api.intents.current(ownKey)?.state==='prepared') api.discardPrepared(ownKey);
      if (mounted.current) setError(failure instanceof ApiFailure?failure:new ApiFailure('unavailable'));
    } finally {
      active.current=false;prepared.current=undefined;hashing.current=undefined;
      if (mounted.current) setBusy(false);
    }
  }
  return <><Link to="/knowledge">{c('Kembali ke dokumen','Back to documents')}</Link>
    <div className="page-heading"><Heading>{replacement?c('Unggah file pengganti','Upload replacement file'):c('Unggah file pengetahuan','Upload a knowledge file')}</Heading><p>{c('Pilih cakupan dan satu file. Server memeriksa file sebelum dapat digunakan untuk membuat draft.','Choose a scope and one file. The server inspects the file before it can be used to create a draft.')}</p></div>
    {!receipt && hasAdmission && <PendingKnowledgeOperations documentId={replacement?.document_id ?? null} />}
    {!receipt && <form className="upload-form" onSubmit={(event) => {event.preventDefault();void submit();}}>
      <CollectionFilter value={collection} onChange={setCollection} allowAll={false} disabled={blocked || !!replacement} />
      <div className="field"><label htmlFor={inputId}>{c('File pengetahuan','Knowledge file')}</label>
        <input key={inputRevision} id={inputId} type="file" accept=".pdf,.txt,.md,.markdown,application/pdf,text/plain,text/markdown" disabled={blocked}
          aria-describedby={inputId+'-hint'} aria-invalid={error?.kind==='input' || undefined}
          onChange={(event) => {setError(undefined);const selected=event.target.files?.[0];try {if (selected) fileMime(selected);setFile(selected);} catch {event.target.value='';setFile(undefined);setError(new ApiFailure('input'));}}} />
        <p id={inputId+'-hint'} className="field-hint">{c('PDF berteks, teks UTF-8 (.txt), atau Markdown UTF-8 (.md). Maksimum 10 MiB; batas layanan dapat lebih kecil.','Text-based PDF, UTF-8 text (.txt), or UTF-8 Markdown (.md). Maximum 10 MiB; the service may have a lower limit.')}</p>
        {file && <p className="upload-filename">{file.name} Â· {fileMime(file)} Â· {new Intl.NumberFormat().format(file.size)} {c('byte','bytes')}</p>}
      </div>
      <div className="workspace-actions"><Button type="submit" variant="primary" disabled={blocked || !file || !collection}>{c('Unggah file','Upload file')}</Button>
        {file && !blocked && <Button type="button" onClick={() => {clearFile();setError(undefined);}}>{c('Batalkan pilihan','Clear selection')}</Button>}
        {busy && phase==='hashing' && <Button type="button" onClick={() => hashing.current?.abort()}>{c('Batalkan persiapan','Cancel preparation')}</Button>}
      </div>
    </form>}
    {descriptor?.purpose==='receipt' && <p><Link to="/reimbursements">{c('Selesaikan unggahan bukti di penggantian biaya.','Finish the pending receipt upload in reimbursements.')}</Link></p>}
    {descriptor && <p className="field-hint">{c('Cakupan unggahan: ','Upload scope: ')}{descriptor.collection_id ?? c('Dokumen tingkat perusahaan','Company-level documents')} Â· {descriptor.mime}</p>}
    {(busy || intent?.state==='dispatching') && <StatusMessage kind="loading" title={busy && phase==='hashing'?c('Menyiapkan file','Preparing file'):c('Mengirim file','Sending file')}><p>{c('Tunggu hasil server sebelum memulai unggahan lain.','Wait for the server result before starting another upload.')}</p></StatusMessage>}
    {(unknown || error) && <StatusMessage kind="error" title={unknown?c('Hasil unggahan belum diketahui','Upload result unknown'):c('Unggahan belum selesai','Upload incomplete')}>
      <p>{unknown?c('Periksa permintaan yang sama sebelum mengunggah file lain. Pemeriksaan menggunakan file dan kunci unggahan semula.','Check the same request before uploading another file. This check uses the original file and upload key.'):error?.kind==='input'?c('Pilih file yang tidak kosong dengan jenis dan ukuran yang didukung. Server juga memeriksa format dan isinya.','Choose a nonempty file with a supported type and size. The server also checks its format and contents.'):error?.kind==='cancelled'?c('Persiapan dibatalkan sebelum file dikirim.','Preparation was cancelled before the file was sent.'):c('Periksa koneksi, cakupan, dan ketersediaan layanan sebelum mencoba kembali.','Check your connection, scope, and service availability before trying again.')}</p>
      {error?.requestId && <p>{c('ID permintaan: ','Request ID: ')}{error.requestId}</p>}
      {unknown && descriptor?.purpose!=='receipt' && <Button disabled={busy} onClick={() => {void submit(true);}}>{c('Periksa hasil unggahan','Check upload result')}</Button>}
    </StatusMessage>}
    {receipt && <><FileInspection receipt={receipt} admitted={admitted} collectionId={collection==='tenant'?null:collection} replacement={replacement} onAdmitted={() => {setAdmitted(true);onAdmitted?.();}} /><Button disabled={hasAdmission} onClick={() => {setReceipt(undefined);setAdmitted(false);setError(undefined);clearFile();}}>{c('Pilih file lain','Choose another file')}</Button></>}
    <p className="session-note">{c('Unggah tidak membuat atau menerbitkan dokumen. Ekstraksi teks dan indeks pengetahuan dimulai setelah pembuatan draft. OCR dan pemrosesan ulang otomatis tidak tersedia.','Uploading does not create or publish a document. Text extraction and knowledge indexing start after draft creation. OCR and automatic reprocessing are unavailable.')}</p>
  </>;
}
