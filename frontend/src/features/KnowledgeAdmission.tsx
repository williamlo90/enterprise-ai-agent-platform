import { useCallback,useState } from 'react';
import { Link } from 'react-router';
import { ApiFailure } from '../api/failure';
import { Button } from '../components/Button';
import { CheckboxField,SelectField,TextField } from '../components/Fields';
import { StatusMessage } from '../components/StatusMessage';
import { useUnsavedInput } from '../app/unsaved-context';
import { AudienceSelection } from './AudienceSelection';
import type { FrozenSelection } from './audience-api';
import type { KnowledgeDocument } from './knowledge-api';
import type { KnowledgeFile } from './upload-api';
import { initialAdmissionBody,periodInput } from './knowledge-lifecycle-api';
import type { AdmissionReceipt } from './knowledge-lifecycle-api';
import { useKnowledgeOperation } from './use-knowledge-operation';
import { useCopy } from './workspace-hooks';
import { WriteState } from './workspace';

export function KnowledgeAdmission({file,collectionId,replacement,onAdmitted}:{file:KnowledgeFile;collectionId:string|null;replacement?:KnowledgeDocument;onAdmitted:()=>void}) {
  const c=useCopy();const [title,setTitle]=useState('');const [from,setFrom]=useState('');const [until,setUntil]=useState('');const [audience,setAudience]=useState<'specific_people'|'all_current_employees'>('specific_people');const [selection,setSelection]=useState<FrozenSelection|null>(null);const [confirm,setConfirm]=useState(false);const [error,setError]=useState(false);const [receipt,setReceipt]=useState<AdmissionReceipt>();
  const action=useKnowledgeOperation(replacement?.document_id ?? null);const ready=useCallback((value:FrozenSelection|null) => {setSelection(value);setConfirm(false);},[]);
  const unrelated=!!action.pending && action.pending.entry.context.fileId!==file.id;
  const blocked=action.busy || !!action.pending;
  useUnsavedInput(!receipt && (!!title || !!from || !!until || confirm || !!selection));
  async function admit(reconcile=false) {
    setError(false);
    try {
      if (unrelated || !reconcile && (!confirm || file.scan_state!=='ready')) throw new ApiFailure('input');
      if (reconcile) {const result=await action.execute();if (result?.kind==='admission') {setReceipt(result.value);onAdmitted();}return;}
      const period=periodInput(from,until);
      if (!reconcile && !replacement && audience==='specific_people' && (!selection || selection.expires_at*1000<=Date.now())) throw new ApiFailure('input');
      const body=replacement?{file_id:file.id,expected_version:replacement.document_version,...period}:initialAdmissionBody(file.id,title,period,audience,selection?.selection_id ?? null);
      const result=await action.execute({method:'POST',path:replacement?'/api/v1/knowledge/documents/'+replacement.document_id+'/versions':'/api/v1/knowledge/documents',body},{kind:replacement?'version':'create',documentId:replacement?.document_id ?? null,collectionId,fileId:file.id,expectedVersionNo:replacement?.latest_version?replacement.latest_version.version_no+1:replacement?undefined:1});
      if (result?.kind==='admission') {setReceipt(result.value);onAdmitted();}
    } catch {setError(true);}
  }
  if (receipt) return <StatusMessage kind="success" title={c('Draft diterima server','Draft admitted by server')}><p>{c('Versi ','Version ')}{receipt.version_no}{c(' tercatat untuk diproses. Periksa status, ekstraksi dan audience sebelum menerbitkan.',' was admitted for processing. Review status, extraction, and audience before publishing.')}</p><Link className="button" to={'/knowledge/documents/'+receipt.document_id}>{c('Periksa draft dokumen','Review document draft')}</Link></StatusMessage>;
  return <section className="processing-status"><h2>{replacement?c('Buat versi pengganti','Create a replacement version'):c('Buat draft dokumen','Create a document draft')}</h2>
    <p>{c('File siap belum menjadi dokumen. Tindakan ini memakai kunci tersendiri dan tidak menerbitkan dokumen.','A ready file is not yet a document. This action uses a separate key and does not publish the document.')}</p>
    <div className="audience-editor">
      {!replacement && <TextField label={c('Judul dokumen','Document title')} value={title} maxLength={200} disabled={blocked} onChange={(event) => {setTitle(event.target.value);setConfirm(false);}} />}
      {replacement && <p>{c('Audience dokumen tetap berlaku pada versi baru dan lama. Versi aktif lama tetap dipakai sampai versi pengganti diterbitkan. Perubahan audience dikelola terpisah.','The document audience applies to new and earlier versions. The previous active version remains in use until its replacement is published. Audience changes are managed separately.')}</p>}
      <div className="workspace-filters"><TextField type="date" label={c('Mulai berlaku, inklusif','Effective from, inclusive')} value={from} disabled={blocked} onChange={(event) => {setFrom(event.target.value);setConfirm(false);}} /><TextField type="date" label={c('Akhir berlaku, eksklusif','Effective until, exclusive')} value={until} disabled={blocked} onChange={(event) => {setUntil(event.target.value);setConfirm(false);}} /></div>
      <p className="field-hint">{c('Kosong berarti belum dicatat. Tanggal berlaku menggunakan Asia/Singapore; versi baru tidak mewarisi tanggal lama secara otomatis. Ini bukan keputusan kelayakan reimbursement.','Empty means not recorded. Effective dates use Asia/Singapore; a new version does not automatically inherit earlier dates. These dates do not determine reimbursement eligibility.')}</p>
      {!replacement && <><SelectField label={c('Audience awal dokumen','Initial document audience')} value={audience} disabled={blocked} options={[{value:'specific_people',label:c('Orang tertentu','Specific people')},{value:'all_current_employees',label:c('Seluruh karyawan','All employees')}]} onChange={(event) => {setAudience(event.target.value as typeof audience);setSelection(null);setConfirm(false);}} />
        {audience==='specific_people'?<AudienceSelection collectionId={collectionId} base={null} onReady={ready} disabled={blocked} />:<p>{c('Setelah diterbitkan, sumber dapat dibaca current employee perusahaan sesuai pemeriksaan server. Draft tetap manager-only.','After publishing, sources are available to the company’s current employees subject to server checks. Drafts remain manager-only.')}</p>}
      </>}
      <CheckboxField checked={confirm} disabled={blocked || !replacement && audience==='specific_people' && !selection} label={c('Saya telah memeriksa judul, periode dan audience draft ini','I reviewed this draft’s title, effective period, and audience')} onChange={(event) => setConfirm(event.target.checked)} />
      <Button type="button" variant="primary" onClick={() => {void admit();}} disabled={blocked || !confirm || !replacement && (!title.trim() || audience==='specific_people' && !selection)}>{replacement?c('Buat versi draft','Create draft version'):c('Buat draft','Create draft')}</Button>
    </div>
    <WriteState busy={action.busy} error={action.error} unknown={action.unknown && !unrelated} reconcile={() => {void admit(true);}} />
    {unrelated && <StatusMessage kind="error" title={c('Draft lain belum selesai','Another draft request is unresolved')}><Link to="/knowledge">{c('Periksa tindakan di katalog dokumen','Check pending actions in the catalog')}</Link></StatusMessage>}
    {error && <StatusMessage kind="error" title={c('Periksa isian draft','Check draft fields')}><p>{c('Periksa judul, urutan tanggal, status file dan pilihan pembaca yang belum kedaluwarsa.','Check the title, date order, file readiness, and unexpired reader selection.')}</p></StatusMessage>}
  </section>;
}
