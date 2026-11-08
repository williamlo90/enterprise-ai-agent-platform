import { useCallback,useEffect,useRef,useState } from 'react';
import { ApiFailure } from '../api/failure';
import { decodeWrite } from '../api/write-policy';
import { Button } from '../components/Button';
import { CheckboxField,TextField } from '../components/Fields';
import { StatusMessage } from '../components/StatusMessage';
import { useUnsavedInput } from '../app/unsaved-context';
import { decodeDirectory,directoryPath,emptyReaderFilters,selectionBody,selectionReceiptDecoder } from './audience-api';
import type { Audience,FrozenSelection,Reader,ReaderFilters } from './audience-api';
import { loadSelection } from './audience-load';
import { ReaderList } from './ReaderList';
import { ResourceState,WriteState } from './workspace';
import { useCopy,useResource,useWorkspace,useWrite } from './workspace-hooks';

// Reused for admission with base=null; the server-added creator is reviewed
// explicitly before the parent receives a usable selection receipt.
export function AudienceSelection({collectionId,base,onReady,disabled=false}:{collectionId:string|null;base:Audience|null;onReady:(selection:FrozenSelection|null)=>void;disabled?:boolean}) {
  const c=useCopy();const {api,stamp}=useWorkspace();
  const [filters,setFilters]=useState<ReaderFilters>({...emptyReaderFilters});const [query,setQuery]=useState<ReaderFilters>({...emptyReaderFilters});const [cursors,setCursors]=useState<string[]>([]);
  const [selected,setSelected]=useState<Reader[]>(base?.readers ?? []);const [excluded,setExcluded]=useState(new Set<string>());
  const [review,setReview]=useState<{receipt:FrozenSelection;readers:Reader[];bulk:boolean}>();const [error,setError]=useState<ApiFailure>();const [reading,setReading]=useState(false);const [accepted,setAccepted]=useState(false);
  const pendingRead=useRef<AbortController | undefined>(undefined);const mounted=useRef(false);const [expired,setExpired]=useState(false);
  const [closed,setClosed]=useState(false);
  const decode=useCallback((value:unknown) => {
    const intent=api.intents.pending('POST /api/v1/knowledge/selections',stamp.generation);
    if (!intent) throw new ApiFailure('malformed');
    return selectionReceiptDecoder(decodeWrite(intent.material).body)(value);
  },[api,stamp.generation]);
  const write=useWrite('POST','/api/v1/knowledge/selections',decode);
  const directory=useResource(directoryPath(collectionId,query,cursors.at(-1)),decodeDirectory);
  const blocked=disabled || reading || write.busy || !!write.intent;
  useUnsavedInput(selected.length>0 || !!review || !!write.intent);
  useEffect(() => {mounted.current=true;return () => {mounted.current=false;pendingRead.current?.abort();};},[]);
  useEffect(() => api.subscribeInvalidation(() => {pendingRead.current?.abort();setClosed(true);setReview(undefined);setSelected([]);onReady(null);}),[api,onReady]);
  useEffect(() => {
    if (!review) return;
    const timer=setTimeout(() => {setExpired(true);setAccepted(false);onReady(null);},Math.max(0,review.receipt.expires_at*1000-Date.now()));return () => clearTimeout(timer);
  },[review,onReady]);
  function dirty() {setReview(undefined);setAccepted(false);setExpired(false);setError(undefined);onReady(null);}
  function toggle(reader:Reader,checked:boolean) {
    dirty();setSelected((rows) => checked?[...rows.filter((row) => row.principal_id!==reader.principal_id),reader].sort((a,b) => a.principal_id.localeCompare(b.principal_id)):rows.filter((row) => row.principal_id!==reader.principal_id));
    setExcluded((ids) => {const next=new Set(ids);if (checked) next.delete(reader.principal_id);else next.add(reader.principal_id);return next;});
  }
  async function freeze(bulk:boolean,reconcile=false) {
    if (reading || closed) return;
    setError(undefined);setAccepted(false);onReady(null);setReview(undefined);
    try {
      const result=await write.execute(reconcile?undefined:selectionBody(collectionId,base,selected,excluded,query,bulk));
      if (!result || !mounted.current) return;
      if (result.base_document_id!==(base?.document_id ?? null) || result.expected_document_version!==(base?.document_version ?? null) || result.collection_id!==collectionId) throw new ApiFailure('conflict');
      const controller=new AbortController();pendingRead.current=controller;setReading(true);
      const readers=await loadSelection(api,stamp,result,controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setReview({receipt:result,readers,bulk});setExpired(result.expires_at*1000<=Date.now());
    } catch (failure) {if (mounted.current) setError(failure instanceof ApiFailure?failure:new ApiFailure('unavailable'));}
    finally {if (mounted.current) setReading(false);}
  }
  const unrelated=write.intent && (() => {try {const body=JSON.parse(write.intent.material).body as Record<string,unknown>;return body.base_document_id!==(base?.document_id ?? null) || body.collection_id!==collectionId;} catch {return true;}})();
  if (closed) return <StatusMessage kind="error" title={c('Konteks telah berubah','Context has changed')} />;
  return <div className="audience-composer">
    <p>{c('Pilihan adalah nama individu. Perubahan departemen, jabatan atau filter tidak mengubah pilihan yang sudah disimpan.','Selections are named individuals. Department, job title, or filter changes do not change a saved selection.')}</p>
    <form className="workspace-filters" onSubmit={(event) => {event.preventDefault();setQuery({...filters});setCursors([]);}}>
      {(['q','department','job_title','group'] as const).map((field) => <TextField key={field} label={field==='q'?c('Cari nama atau ID','Find name or ID'):field==='department'?c('Departemen','Department'):field==='job_title'?c('Jabatan','Job title'):c('Grup','Group')} maxLength={field==='q'?200:100} value={filters[field]} disabled={blocked} onChange={(event) => setFilters({...filters,[field]:event.target.value})} />)}
      <Button type="submit" disabled={blocked}>{c('Terapkan filter pembaca','Apply reader filters')}</Button>
    </form>
    <p className="field-hint">{c('Filter departemen, jabatan dan grup harus cocok tepat dan berlaku bersama. Kosongkan filter untuk mencari nilai lain.','Department, job title and group filters match exactly and apply together. Clear a filter to find other values.')}</p>
    <ResourceState {...directory} retry={directory.refresh} />
    {directory.data && <>{directory.data.items.length===0?<p>{c('Tidak ada nama yang cocok dalam cakupan ini.','No matching people in this scope.')}</p>:<ul className="reader-candidates">{directory.data.items.map((reader) => <li key={reader.principal_id}><CheckboxField disabled={blocked} label={reader.display_name ?? reader.principal_id} hint={reader.display_name?reader.principal_id:undefined} checked={selected.some((row) => row.principal_id===reader.principal_id)} onChange={(event) => toggle(reader,event.target.checked)} /></li>)}</ul>}
      <nav className="workspace-actions" aria-label={c('Halaman pencarian pembaca','Reader search pages')}><Button disabled={blocked || !cursors.length} onClick={() => setCursors(cursors.slice(0,-1))}>{c('Hasil sebelumnya','Previous results')}</Button><Button disabled={blocked || !directory.data.next_after} onClick={() => {if (directory.data?.next_after) setCursors([...cursors,directory.data.next_after]);}}>{c('Hasil berikutnya','Next results')}</Button></nav>
      <Button disabled={blocked} onClick={() => {void freeze(true);}}>{c('Bekukan semua hasil filter','Freeze all matching people')}</Button>
    </>}
    {directory.data && <p role="status">{c('Dipilih pada halaman ini: ','Selected on this page: ')}{directory.data.items.filter((row) => selected.some((item) => item.principal_id===row.principal_id)).length}/{directory.data.items.length}{c(' · Pilihan di halaman lain tetap dipertahankan.',' · Selections on other pages are retained.')}</p>}
    <h3>{c('Pilihan sementara','Working selection')}</h3><ReaderList readers={selected} disabled={blocked} onRemove={(reader) => toggle(reader,false)} />
    <div className="workspace-actions"><Button disabled={blocked} onClick={() => {dirty();setSelected([]);setExcluded(new Set());}}>{c('Kosongkan pilihan','Clear selection')}</Button><Button disabled={blocked} onClick={() => {void freeze(false);}}>{c('Periksa pilihan akhir','Review final selection')}</Button></div>
    <WriteState error={write.error} unknown={write.unknown && !unrelated} busy={write.busy} reconcile={() => {void freeze(false,true);}} />
    {unrelated && <StatusMessage kind="error" title={c('Permintaan pilihan lain belum selesai','Another selection request is unresolved')}><p>{c('Kembali ke dokumen atau cakupan asal untuk memeriksa hasilnya sebelum membuat pilihan baru.','Return to the original document or scope and check its result before making a new selection.')}</p></StatusMessage>}
    {reading && <StatusMessage kind="loading" title={c('Memuat seluruh ringkasan pilihan','Loading the complete selection summary')} />}
    {error && <StatusMessage kind="error" title={c('Pilihan belum dapat digunakan','Selection cannot be used yet')}><p>{error.kind==='conflict'?c('Versi atau kelayakan telah berubah. Muat ulang dan periksa pilihan baru.','The version or eligibility changed. Reload and review a new selection.'):c('Pilihan ditolak atau tidak dapat dimuat utuh. Periksa filter atau kurangi pilihan lalu periksa kembali. Tidak ada daftar yang dipotong atau disimpan sebagian.','The selection was rejected or could not be loaded in full. Check filters or reduce the selection and review again. No list is truncated or saved partially.')}</p></StatusMessage>}
    {review && <section className="audience-review"><h3>{c('Ringkasan pilihan beku','Frozen selection summary')}</h3><ReaderList readers={review.readers} />
      {review.receipt.creator_grant_included && <p>{c('Pembuat dokumen ditambahkan server sebagai pembaca awal. Grant ini dapat dicabut kemudian.','The server added the document creator as an initial reader. This grant can be removed later.')}</p>}
      <p>{c('Nama dan status ini ditangkap saat pilihan dibekukan. Server memeriksa kelayakan lagi saat menyimpan.','These names and statuses were captured when the selection was frozen. The server checks eligibility again when saving.')}</p>
      <p>{c('Berlaku sampai: ','Expires at: ')}{new Date(review.receipt.expires_at*1000).toLocaleString(c('id-ID','en-GB'),{timeZoneName:'short'})}</p>
      {expired?<p role="status">{c('Pilihan kedaluwarsa. Periksa pilihan baru sebelum menyimpan.','Selection expired. Review a new selection before saving.')}</p>:<Button disabled={blocked || accepted} onClick={() => {try {api.identityView(stamp);if (review.receipt.expires_at*1000<=Date.now()) throw new ApiFailure('conflict');setSelected(review.readers);setAccepted(true);onReady(review.receipt);} catch {setExpired(true);onReady(null);}}}>{accepted?c('Pilihan telah diperiksa','Selection reviewed'):review.bulk?c('Pilih semua ','Select all ')+review.receipt.selected_count+c(' orang',' people'):c('Gunakan pilihan ','Use selection of ')+review.receipt.selected_count+c(' orang',' people')}</Button>}
    </section>}
  </div>;
}
