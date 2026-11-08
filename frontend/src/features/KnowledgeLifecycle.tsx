import { useEffect,useRef,useState } from 'react';
import { ApiFailure } from '../api/failure';
import { Button } from '../components/Button';
import { CheckboxField,SelectField } from '../components/Fields';
import { SourceHighlight } from '../components/SourceHighlight';
import { StatusMessage } from '../components/StatusMessage';
import { useUnsavedInput } from '../app/unsaved-context';
import type { KnowledgeDocument,KnowledgeVersion } from './knowledge-api';
import { previewDecoder } from './knowledge-lifecycle-api';
import type { ExtractedPreview } from './knowledge-lifecycle-api';
import { loadAudience } from './audience-load';
import type { Audience } from './audience-api';
import { ReaderList } from './ReaderList';
import { KnowledgeUpload } from './KnowledgeUpload';
import { useCopy,useWorkspace } from './workspace-hooks';
import { useKnowledgeOperation } from './use-knowledge-operation';
import { WriteState } from './workspace';

export function KnowledgeLifecycle({document,versions,refresh}:{document:KnowledgeDocument;versions:KnowledgeVersion[];refresh:()=>void}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const action=useKnowledgeOperation(document.document_id);
  const choices=[...new Map([document.latest_version,document.active_version,...versions].filter((row):row is KnowledgeVersion => !!row).map((row) => [row.version_id,row])).values()];
  const [selected,setSelected]=useState(choices[0]?.version_id ?? '');const version=choices.find((row) => row.version_id===selected);
  const [review,setReview]=useState<{preview:ExtractedPreview;audience:Audience}>();const [readError,setReadError]=useState<ApiFailure>();const [reading,setReading]=useState(false);const [confirmed,setConfirmed]=useState(false);const [withdraw,setWithdraw]=useState(false);const [replacement,setReplacement]=useState(false);
  const mounted=useRef(false);const read=useRef<AbortController | undefined>(undefined);const activeRead=useRef(false);
  const blocked=reading || action.busy || !!action.pending;
  useUnsavedInput(confirmed || withdraw || replacement);
  useEffect(() => {mounted.current=true;const unsubscribe=api.subscribeInvalidation(() => {read.current?.abort();setReview(undefined);setConfirmed(false);});return () => {mounted.current=false;read.current?.abort();unsubscribe();};},[api]);
  function clearReview() {read.current?.abort();setReview(undefined);setConfirmed(false);setReadError(undefined);}
  async function inspect() {
    if (!version || activeRead.current || blocked) return;
    activeRead.current=true;clearReview();setReading(true);const controller=new AbortController();read.current=controller;
    try {
      const preview=await api.resourceRead({path:'/api/v1/knowledge/documents/'+document.document_id+'/versions/'+version.version_id+'/preview',decode:previewDecoder(document,version)},stamp,controller.signal);
      const audience=await loadAudience(api,stamp,document.document_id,controller.signal);
      if (audience.document_version!==document.document_version || audience.access_revision!==preview.access_revision) throw new ApiFailure('conflict');
      if (mounted.current && !controller.signal.aborted) setReview({preview,audience});
    } catch (error) {if (mounted.current && !controller.signal.aborted) setReadError(error instanceof ApiFailure?error:new ApiFailure('unavailable'));}
    finally {activeRead.current=false;if (mounted.current) setReading(false);}
  }
  async function mutate(kind:'publish'|'withdraw',reconcile=false) {
    if (!reconcile && (kind==='publish'?!review || !confirmed || !version:!withdraw)) return;
    clearReview();setReplacement(false);
    const result=await action.execute(reconcile?undefined:{method:'POST',path:kind==='publish'?'/api/v1/knowledge/versions/'+version!.version_id+'/publish':'/api/v1/knowledge/documents/'+document.document_id+'/withdraw',body:{expected_version:document.document_version}},reconcile?undefined:{kind,documentId:document.document_id,collectionId:document.collection_id,accessRevision:document.access_revision,...(kind==='publish'?{versionId:version!.version_id}:{})});
    if (result) refresh();
  }
  const ready=!!version && version.parse_state==='ready' && version.index_state==='ready';
  const publication=(state:string) => state==='draft'?c('Draft','Draft'):state==='published'?c('Diterbitkan','Published'):state==='superseded'?c('Versi lama','Previous version'):c('Ditarik','Withdrawn');
  return <section className="processing-status"><h2>{c('Periksa dan terbitkan versi','Review and publish a version')}</h2>
    <SelectField label={c('Versi yang diperiksa','Version to review')} value={selected} disabled={blocked} options={choices.map((row) => ({value:row.version_id,label:c('Versi ','Version ')+row.version_no+' · '+publication(row.publication_state)}))} onChange={(event) => {clearReview();setSelected(event.target.value);}} />
    <p>{c('Periksa teks ekstraksi, versi, periode dan audience sebelum menerbitkan. Persetujuan pembaca tidak diperlukan.','Review extracted text, version, effective period, and audience before publishing. Reader approval is not required.')}</p>
    <div className="workspace-actions"><Button disabled={blocked || !ready} onClick={() => {void inspect();}}>{c('Buka preview ekstraksi','Open extraction preview')}</Button><Button disabled={blocked} onClick={() => {clearReview();refresh();}}>{c('Muat ulang versi dan status','Reload version and status')}</Button></div>
    {!ready && <p role="status">{c('Versi belum siap diperiksa atau pemrosesan gagal. Periksa status server; perbaiki file asal bila perlu. Tidak ada OCR, editor ekstraksi, atau tombol pemrosesan ulang.','The version is not ready for review or processing failed. Check server status and correct the original file if needed. OCR, extraction editing, and reprocessing controls are unavailable.')}</p>}
    {reading && <StatusMessage kind="loading" title={c('Memeriksa ekstraksi dan audience','Checking extraction and audience')} />}
    {readError && <StatusMessage kind="error" title={readError.kind==='not_found'?c('Preview tidak tersedia','Preview unavailable'):c('Preview belum dapat diperiksa','Preview could not be verified')}><p>{c('Muat ulang detail untuk memeriksa versi dan kewenangan terbaru. Preview gagal tidak berarti versi siap diterbitkan.','Reload the detail to check the current version and authority. A failed preview does not mean the version is ready to publish.')}</p></StatusMessage>}
    {review && <section className="extraction-preview"><h3>{review.preview.title}</h3><p>{c('Preview pengelola · Versi ','Manager preview · Version ')}{review.preview.version_no}</p>
      <dl className="workspace-facts"><div><dt>{c('Publikasi','Publication')}</dt><dd>{publication(review.preview.publication_state)}</dd></div><div><dt>{c('Mulai berlaku, inklusif','Effective from, inclusive')}</dt><dd>{review.preview.valid_from ?? c('Belum dicatat','Not recorded')}</dd></div><div><dt>{c('Akhir berlaku, eksklusif','Effective until, exclusive')}</dt><dd>{review.preview.valid_until ?? c('Belum dicatat','Not recorded')}</dd></div></dl>
      <p className="field-hint">{c('Tanggal mengikuti Asia/Singapore. Preview ini adalah hasil ekstraksi, bukan tampilan PDF atau keputusan kelayakan transaksi.','Dates use Asia/Singapore. This is extracted text, rather than a PDF rendering or a transaction eligibility decision.')}</p>
      {review.preview.chunks.map((chunk) => <section key={chunk.chunk_no}><h4>{c('Halaman ','Page ')+chunk.page+c(' · Bagian ',' · Chunk ')+chunk.chunk_no}</h4><SourceHighlight text={chunk.text} /></section>)}
      <h3>{c('Audience versi ini dan seluruh riwayat','Audience for this version and all history')}</h3><p>{review.audience.audience_mode==='all_current_employees'?c('Seluruh karyawan aktif saat pemeriksaan akses.','All current employees at access time.'):c('Orang tertentu sesuai daftar eksplisit.','Specific people in the explicit list.')}</p>
      {review.audience.audience_mode==='specific_people' && <ReaderList readers={review.audience.readers} />}
      <p>{c('Pengelola berwenang tetap mempunyai akses terpisah. Perubahan audience dikelola pada panel pembaca.','Authorized managers retain separate access. Audience changes are managed in the readers panel.')}</p>
      {version?.publication_state==='draft' && <><CheckboxField label={c('Saya telah memeriksa ekstraksi, periode dan audience versi ini','I reviewed this version’s extraction, period, and audience')} checked={confirmed} disabled={blocked} onChange={(event) => setConfirmed(event.target.checked)} /><Button variant="primary" disabled={blocked || !confirmed} onClick={() => {void mutate('publish');}}>{c('Terbitkan versi ini','Publish this version')}</Button></>}
    </section>}
    <WriteState error={action.unknown?undefined:action.error} unknown={false} busy={action.busy} reconcile={() => {void mutate('publish',true);}} />
    {action.error?.kind==='conflict' && !action.unknown && <Button onClick={refresh}>{c('Muat ulang dan periksa kembali','Reload and review again')}</Button>}
    <div className="workspace-actions"><Button disabled={blocked} onClick={() => {clearReview();setReplacement((value) => !value);}}>{replacement?c('Tutup unggahan pengganti','Close replacement upload'):c('Unggah versi pengganti','Upload replacement version')}</Button></div>
    {replacement && <KnowledgeUpload replacement={document} onAdmitted={refresh} />}
    <div className="withdraw-controls"><CheckboxField label={c('Saya memahami penarikan dokumen bersifat terminal untuk seluruh versi','I understand document withdrawal is terminal for all versions')} checked={withdraw} disabled={blocked} onChange={(event) => setWithdraw(event.target.checked)} /><Button variant="danger" disabled={blocked || !withdraw} onClick={() => {void mutate('withdraw');}}>{c('Tarik seluruh dokumen','Withdraw entire document')}</Button></div>
  </section>;
}
