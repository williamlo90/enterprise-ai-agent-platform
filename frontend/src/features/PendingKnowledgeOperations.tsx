import { useEffect,useRef,useState,useSyncExternalStore } from 'react';
import { Link,useNavigate } from 'react-router';
import { ApiFailure } from '../api/failure';
import { decodeWrite } from '../api/write-policy';
import { Button } from '../components/Button';
import { StatusMessage } from '../components/StatusMessage';
import { knowledgeExecute,knowledgePending } from './knowledge-operations';
import { useCopy,useWorkspace } from './workspace-hooks';

// Mounted outside document data, so a terminal404 cannot hide reconciliation.
export function PendingKnowledgeOperations({documentId,onSettled}:{documentId?:string|null;onSettled?:()=>void}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const navigate=useNavigate();const [busy,setBusy]=useState<string>();const [error,setError]=useState<ApiFailure>();const [withdrawn,setWithdrawn]=useState(false);
  useSyncExternalStore(api.intents.subscribe,api.intents.getSnapshot);
  const mounted=useRef(false);useEffect(() => {mounted.current=true;return () => {mounted.current=false;};},[]);
  const rows=knowledgePending(api,stamp).filter((row) => documentId===undefined || row.entry.context.documentId===documentId);
  async function reconcile(key:string) {
    if (busy) return;setBusy(key);setError(undefined);
    try {
      const receipt=await knowledgeExecute(api,stamp,undefined,undefined,key);
      if (!mounted.current) return;
      if (receipt.kind==='lifecycle' && receipt.value.withdrawn) {if (documentId===undefined || documentId===null) setWithdrawn(true);onSettled?.();}
      else {const id=receipt.value.document_id;void navigate('/knowledge/documents/'+id);onSettled?.();}
    } catch (failure) {if (mounted.current) setError(failure instanceof ApiFailure?failure:new ApiFailure('unavailable'));}
    finally {if (mounted.current) setBusy(undefined);}
  }
  return <>{withdrawn && <StatusMessage kind="success" title={c('Dokumen telah ditarik','Document withdrawn')}><p>{c('Penarikan bersifat terminal. Gunakan dokumen baru untuk penggantinya.','Withdrawal is terminal. Use a new document for its replacement.')}</p><Link to="/knowledge">{c('Kembali ke dokumen','Back to documents')}</Link></StatusMessage>}
    {rows.map(({entry,intent}) => <StatusMessage key={entry.key} kind="error" title={intent.state==='unknown'?c('Hasil tindakan dokumen belum diketahui','Document action result unknown'):c('Tindakan dokumen sedang diproses','Document action in progress')}>
      <p>{entry.context.kind==='create'?c('Pembuatan draft','Draft creation'):entry.context.kind==='version'?c('Pembuatan versi pengganti','Replacement version creation'):entry.context.kind==='publish'?c('Publikasi','Publication'):c('Penarikan dokumen','Document withdrawal')}</p>
      {entry.context.documentId && <Link to={'/knowledge/documents/'+entry.context.documentId}>{c('Buka dokumen','Open document')}</Link>}
      {typeof decodeWrite(intent.material).body.title==='string' && <p>{decodeWrite(intent.material).body.title as string}</p>}
      <p>{c('Periksa permintaan yang sama sebelum membuat tindakan baru. Status tidak tersedia atau404 tidak membuktikan hasil tindakan.','Check the same request before starting a new action. Unavailable status or404 does not prove its outcome.')}</p>
      {intent.state==='unknown' && <Button disabled={!!busy} onClick={() => {void reconcile(entry.key);}}>{c('Rekonsiliasi tindakan dokumen','Reconcile document action')}</Button>}
    </StatusMessage>)}
    {error && <StatusMessage kind="error" title={c('Hasil belum dapat diperiksa','Result could not be checked')}><p>{c('Periksa koneksi dan kewenangan Anda, lalu periksa permintaan yang sama lagi.','Check your connection and authority, then check the same request again.')}</p></StatusMessage>}
  </>;
}
