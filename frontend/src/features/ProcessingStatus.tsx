import { useMemo,useState } from 'react';
import { Button } from '../components/Button';
import { StatusMessage } from '../components/StatusMessage';
import type { PollState } from './status-poll';
import { processingDecoder } from './upload-api';
import type { ProcessingStatus as Processing } from './upload-api';
import type { KnowledgeVersion } from './knowledge-api';
import { useStatusPoll } from './use-status-poll';
import { useCopy } from './workspace-hooks';

export function PollFeedback<T>({status}:{status:PollState<T>&{retryBlocked:boolean;refresh:()=>void}}) {
  const c=useCopy();
  return <>
    {status.phase==='loading' && <StatusMessage kind="loading" title={c('Memeriksa status terbaru','Checking current status')} />}
    {status.phase==='error' && <StatusMessage kind="error" title={status.error?.kind==='not_found'?c('Tidak tersedia','Unavailable'):c('Status belum dapat dimuat','Status could not be loaded')}>
      <p>{status.error?.kind==='not_found'?c('File atau versi tidak tersedia, atau akses Anda telah berubah.','The file or version is unavailable, or your access has changed.'):c('Pemeriksaan otomatis berhenti. Coba periksa kembali setelah layanan tersedia.','Automatic checking stopped. Check again when the service is available.')}</p>
      {status.error?.requestId && <p>{c('ID permintaan: ','Request ID: ')}{status.error.requestId}</p>}
    </StatusMessage>}
    {status.phase==='bounded' && <p role="status">{c('Pemeriksaan otomatis selesai untuk saat ini. Pemrosesan server dapat tetap berjalan.','Automatic checking has stopped for now. Server processing may continue.')}</p>}
    {status.phase==='paused' && <p role="status">{c('Pemeriksaan berhenti saat halaman tidak aktif atau koneksi terputus. Periksa kembali setelah kembali terhubung.','Checking stopped while the page was inactive or offline. Check again once connected.')}</p>}
    {['error','bounded','paused'].includes(status.phase) && <Button disabled={status.retryBlocked} onClick={status.refresh}>{status.retryBlocked?c('Tunggu sebelum memeriksa kembali','Wait before checking again'):c('Periksa status lagi','Check status again')}</Button>}
  </>;
}
export function VersionProcessing({version,reload}:{version:KnowledgeVersion;reload:()=>void}) {
  const c=useCopy();const [monitor,setMonitor]=useState(false);
  return <section className="processing-status"><h2>{c('Pantau pemrosesan versi terbaru','Monitor latest version processing')}</h2>
    {monitor?<CurrentProcessing version={version} reload={reload} />:<Button onClick={() => setMonitor(true)}>{c('Periksa pemrosesan versi','Check version processing')}</Button>}
  </section>;
}
function CurrentProcessing({version,reload}:{version:KnowledgeVersion;reload:()=>void}) {
  const c=useCopy();const decode=useMemo(() => processingDecoder(version.document_id,version.version_id,version.version_no),[version.document_id,version.version_id,version.version_no]);
  const terminal=useMemo(() => (value:Processing) => value.document_version!==version.document_version || value.parse_state==='failed' || value.index_state==='failed' || value.parse_state==='ready' && value.index_state==='ready',[version.document_version]);
  const status=useStatusPoll('/api/v1/knowledge/documents/'+version.document_id+'/versions/'+version.version_id+'/status',decode,terminal);
  const row=status.data;const consistent=row?.document_version===version.document_version;
  return <><PollFeedback status={status} />
    {row && !consistent && <StatusMessage kind="error" title={c('Dokumen telah berubah','Document has changed')}><p>{c('Muat ulang detail sebelum melanjutkan.','Reload the detail before continuing.')}</p><Button onClick={reload}>{c('Muat ulang dokumen','Reload document')}</Button></StatusMessage>}
    {row && consistent && <><dl className="workspace-facts">
      <div><dt>{c('Ekstraksi teks','Text extraction')}</dt><dd>{row.parse_state==='ready'?c('Siap','Ready'):row.parse_state==='failed'?c('Gagal','Failed'):row.parse_state==='processing'?c('Diproses','Processing'):c('Menunggu','Pending')}</dd></div>
      <div><dt>{c('Indeks pengetahuan','Knowledge index')}</dt><dd>{row.index_state==='ready'?c('Siap','Ready'):row.index_state==='failed'?c('Gagal','Failed'):row.index_state==='building'?c('Dibangun','Building'):c('Menunggu','Pending')}</dd></div>
      <div><dt>{c('Publikasi','Publication')}</dt><dd>{row.publication_state==='published'?c('Diterbitkan','Published'):row.publication_state==='superseded'?c('Versi lama','Previous version'):c('Draft','Draft')}</dd></div>
    </dl>{(row.parse_state==='failed' || row.index_state==='failed') && <p role="status">{c('Pemrosesan gagal. Periksa file asal; versi ini tidak siap diterbitkan. Tidak ada pemrosesan ulang otomatis.','Processing failed. Check the original file; this version is not ready to publish. There is no automatic reprocessing.')}</p>}</>}
  </>;
}
