import { useCallback,useEffect,useState } from 'react';
import { ApiFailure } from '../api/failure';
import { decodeWrite } from '../api/write-policy';
import { Button } from '../components/Button';
import { CheckboxField,SelectField } from '../components/Fields';
import { useUnsavedInput } from '../app/unsaved-context';
import { AudienceSelection } from './AudienceSelection';
import { ReaderList } from './ReaderList';
import { audienceReceiptDecoder } from './audience-api';
import type { Audience,AudienceMode,FrozenSelection } from './audience-api';
import { loadAudience } from './audience-load';
import { useCopy,useWorkspace,useWrite } from './workspace-hooks';
import { ResourceState,WriteState } from './workspace';

export function DocumentAudience({documentId,collectionId,onChanged}:{documentId:string;collectionId:string|null;onChanged:()=>void}) {
  const c=useCopy();const [open,setOpen]=useState(false);
  return <section className="processing-status"><h2>{c('Audience dan pembaca','Audience and readers')}</h2>{open?<AudienceResource documentId={documentId} collectionId={collectionId} onChanged={onChanged} />:<Button onClick={() => setOpen(true)}>{c('Periksa dan kelola pembaca','Review and manage readers')}</Button>}</section>;
}
function AudienceResource({documentId,collectionId,onChanged}:{documentId:string;collectionId:string|null;onChanged:()=>void}) {
  const {api,stamp}=useWorkspace();const [revision,setRevision]=useState(0);
  const identity=stamp.generation+':'+documentId+':'+revision;
  const [snapshot,setSnapshot]=useState<{identity:string;data?:Audience;error?:ApiFailure}>();
  useEffect(() => {
    let current=true;const controller=new AbortController();
    void loadAudience(api,stamp,documentId,controller.signal).then((data) => {if (current) setSnapshot({identity,data});}).catch((error:unknown) => {if (current) setSnapshot({identity,error:error instanceof ApiFailure?error:new ApiFailure('unavailable')});});
    return () => {current=false;controller.abort();};
  },[api,stamp,documentId,identity]);
  const state=snapshot?.identity===identity?snapshot:undefined;
  const refresh=() => setRevision((value) => value+1);
  return <><ResourceState loading={!state} error={state?.error} retry={refresh} />{state?.data && <><AudienceEditor key={identity+':'+state.data.document_version} current={state.data} collectionId={collectionId} refresh={refresh} reloadDetail={onChanged} /></>}</>;
}
function AudienceEditor({current,collectionId,refresh,reloadDetail}:{current:Audience;collectionId:string|null;refresh:()=>void;reloadDetail:()=>void}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const [editing,setEditing]=useState(false);const [audienceMode,setMode]=useState<AudienceMode>(current.audience_mode);const [selection,setSelection]=useState<FrozenSelection|null>(null);const [confirm,setConfirm]=useState(false);
  const ready=useCallback((value:FrozenSelection|null) => {setSelection(value);setConfirm(false);},[]);
  const path='/api/v1/knowledge/documents/'+current.document_id+'/audience';
  const decode=useCallback((value:unknown) => {
    const intent=api.intents.pending('PATCH '+path,stamp.generation);
    if (!intent) throw new ApiFailure('malformed');
    return audienceReceiptDecoder(current.document_id,decodeWrite(intent.material).body)(value);
  },[api,path,stamp.generation,current.document_id]);
  const write=useWrite('PATCH',path,decode);
  const blocked=write.busy || !!write.intent;
  useUnsavedInput(editing || blocked);
  async function save(reconcile=false) {
    if (!reconcile && (!confirm || audienceMode==='specific_people' && (!selection || selection.expires_at*1000<=Date.now()))) return;
    const receipt=await write.execute(reconcile?undefined:{audience_mode:audienceMode,selection_id:audienceMode==='specific_people'?selection!.selection_id:null,expected_version:current.document_version});
    if (!receipt) return;
    // A replay receipt is historical. Always reload current ACL after ack.
    refresh();
  }
  return <>
    <p>{current.audience_mode==='all_current_employees'?c('Audience saat ini: Seluruh karyawan','Current audience: All employees'):c('Audience saat ini: Orang tertentu','Current audience: Specific people')}</p>
    <p className="field-hint">{c('Pengelola pengetahuan yang berwenang tetap mempunyai akses terpisah. Audience berlaku pada seluruh versi, termasuk riwayat yang masih dapat dibaca.','Authorized knowledge managers retain separate access. Audience applies to all versions, including readable history.')}</p>
    {!editing && !write.intent && <><div className="workspace-actions"><Button onClick={() => setEditing(true)}>{c('Ubah audience','Change audience')}</Button><Button onClick={reloadDetail}>{c('Muat ulang detail dokumen','Reload document detail')}</Button></div>{current.audience_mode==='specific_people' && <ReaderList readers={current.readers} />}</>}
    {(editing || write.intent) && <div className="audience-editor">
      <SelectField label={c('Siapa yang boleh membaca','Who can read')} value={audienceMode} disabled={blocked} options={[{value:'all_current_employees',label:c('Seluruh karyawan','All employees')},{value:'specific_people',label:c('Orang tertentu','Specific people')}]} onChange={(event) => {setMode(event.target.value as AudienceMode);setSelection(null);setConfirm(false);}} />
      {audienceMode==='all_current_employees'?<p>{c('Akses mengikuti karyawan aktif perusahaan saat sumber dibaca. Daftar grant individu akan dikosongkan. Draft tetap hanya dapat dibaca pengelola.','Access follows current active employees when a source is read. Individual grants will be cleared. Drafts remain manager-only.')}</p>:<AudienceSelection collectionId={collectionId} base={current} onReady={ready} disabled={blocked} />}
      <p>{c('Menambah pembaca dapat membuka versi lama. Menghapus nama mencabut grant eksplisit semua versi; kewenangan pengelola tetap terpisah. Untuk audience berbeda tanpa membuka riwayat, gunakan dokumen terpisah.','Adding readers can expose earlier versions. Removing a name revokes its explicit grants across all versions; manager authority stays separate. Use a separate document for a different audience without exposing its history.')}</p>
      <CheckboxField label={c('Saya telah memeriksa audience dan dampaknya pada seluruh versi','I reviewed the audience and its effect on all versions')} checked={confirm} disabled={blocked || audienceMode==='specific_people' && !selection} onChange={(event) => setConfirm(event.target.checked)} />
      <div className="workspace-actions"><Button variant="primary" disabled={blocked || !confirm || audienceMode==='specific_people' && !selection} onClick={() => {void save();}}>{c('Simpan audience','Save audience')}</Button><Button disabled={blocked} onClick={() => {setEditing(false);setSelection(null);setConfirm(false);}}>{c('Batalkan perubahan','Cancel changes')}</Button></div>
    </div>}
    <WriteState error={write.error} unknown={write.unknown} busy={write.busy} reconcile={() => {void save(true);}} />
    {write.error?.kind==='conflict' && !write.unknown && <Button onClick={refresh}>{c('Muat ulang audience untuk diperiksa','Reload audience for review')}</Button>}
  </>;
}
