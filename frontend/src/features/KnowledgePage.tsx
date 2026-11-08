import { useMemo,useState,useSyncExternalStore } from 'react';
import { Link } from 'react-router';
import { TextField } from '../components/Fields';
import { Button } from '../components/Button';
import { StatusMessage } from '../components/StatusMessage';
import { CollectionFilter } from './CollectionFilter';
import { decodeCatalog,documentDecoder,decodeVersions } from './knowledge-api';
import type { KnowledgeDocument,KnowledgeVersion } from './knowledge-api';
import { useCopy,useResource } from './workspace-hooks';
import { ResourceState } from './workspace';
import { VersionProcessing } from './ProcessingStatus';
import { DocumentAudience } from './DocumentAudience';
import { PendingKnowledgeOperations } from './PendingKnowledgeOperations';
import { KnowledgeLifecycle } from './KnowledgeLifecycle';
import { knowledgePending,knowledgeWithdrawn } from './knowledge-operations';
import { useWorkspace } from './workspace-hooks';

export function KnowledgeCatalog() {
  const c=useCopy();const [title,setTitle]=useState('');const [collection,setCollection]=useState('');const [query,setQuery]=useState('');const [cursors,setCursors]=useState<string[]>([]);
  const params=new URLSearchParams(query);const after=cursors.at(-1);if (after) params.set('after',after);params.set('limit','20');
  const result=useResource('/api/v1/knowledge/documents?'+params.toString(),decodeCatalog);
  return <><div className="page-heading"><h1>{c('Dokumen','Documents')}</h1><p>{c('Dokumen dan draft dalam cakupan yang Anda kelola. Versi draft tidak dapat dibaca karyawan.','Documents and drafts within your managed scope. Draft versions are not readable by employees.')}</p></div>
    <PendingKnowledgeOperations />
    <Link className="button button--primary" to="/knowledge/upload">{c('Unggah file','Upload file')}</Link>
    <form className="workspace-filters" onSubmit={(event) => {event.preventDefault();const params=new URLSearchParams();if (title.trim()) params.set('q',title.trim());if (collection) params.set('collection',collection);setQuery(params.toString());setCursors([]);}}>
      <TextField label={c('Cari judul dokumen','Find a document title')} value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} />
      <CollectionFilter value={collection} onChange={setCollection} /><Button type="submit">{c('Terapkan filter','Apply filters')}</Button>
    </form>
    <ResourceState {...result} retry={result.refresh} />
    {result.data && <>{result.data.items.length===0?<StatusMessage kind="empty" title={c('Belum ada dokumen yang cocok','No matching documents')}><p>{c('Ubah filter atau periksa kembali setelah dokumen dibuat.','Change the filters or check again after a document is created.')}</p></StatusMessage>:
      <ul className="workspace-list">{result.data.items.map((document) => <li key={document.document_id}><div><Link to={'/knowledge/documents/'+document.document_id}>{document.title}</Link><p className="field-hint">{document.collection_id ?? c('Dokumen tingkat perusahaan','Company-level document')}</p><p>{document.active_version?c('Aktif: versi ','Active: version ')+document.active_version.version_no:c('Belum diterbitkan','Not published')}</p>{document.latest_version && <p className="field-hint">{c('Terbaru: versi ','Latest: version ')+document.latest_version.version_no+' Ãƒâ€šÃ‚Â· '}<Publication state={document.latest_version.publication_state} /></p>}</div><Link className="button" to={'/knowledge/documents/'+document.document_id}>{c('Periksa dokumen','View document')}</Link></li>)}</ul>}
      <nav className="workspace-actions" aria-label={c('Halaman dokumen','Document pages')}><Button disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0,-1))}>{c('Sebelumnya','Previous')}</Button><Button disabled={!result.data.next_after} onClick={() => {if (result.data?.next_after) setCursors([...cursors,result.data.next_after]);}}>{c('Berikutnya','Next')}</Button></nav></>}
  </>;
}

export function Publication({state}:{state:KnowledgeVersion['publication_state']}) {
  const c=useCopy();return <>{state==='draft'?c('Draft','Draft'):state==='published'?c('Diterbitkan','Published'):state==='superseded'?c('Versi lama','Previous version'):c('Ditarik','Withdrawn')}</>;
}

export function VersionSummary({version}:{version:KnowledgeVersion}) {
  const c=useCopy();
  return <dl className="workspace-facts"><div><dt>{c('Publikasi','Publication')}</dt><dd><Publication state={version.publication_state} /></dd></div>
    <div><dt>{c('Ekstraksi teks','Text extraction')}</dt><dd>{version.parse_state==='pending'?c('Menunggu','Pending'):version.parse_state==='processing'?c('Diproses','Processing'):version.parse_state==='ready'?c('Siap','Ready'):c('Gagal','Failed')}</dd></div>
    <div><dt>{c('Indeks pengetahuan','Knowledge index')}</dt><dd>{version.index_state==='pending'?c('Menunggu','Pending'):version.index_state==='building'?c('Dibangun','Building'):version.index_state==='ready'?c('Siap','Ready'):c('Gagal','Failed')}</dd></div>
    <div><dt>{c('Mulai berlaku','Effective from')}</dt><dd>{version.valid_from ?? c('Belum dicatat','Not recorded')}</dd></div><div><dt>{c('Akhir berlaku, eksklusif','Effective until, exclusive')}</dt><dd>{version.valid_until ?? c('Belum dicatat','Not recorded')}</dd></div>
  </dl>;
}

function VersionHistoryRow({version}:{version:KnowledgeVersion}) {
  const c=useCopy();
  const parse=version.parse_state==='pending'?c('Menunggu','Pending'):version.parse_state==='processing'?c('Diproses','Processing'):version.parse_state==='ready'?c('Siap','Ready'):c('Gagal','Failed');
  const index=version.index_state==='pending'?c('Menunggu','Pending'):version.index_state==='building'?c('Dibangun','Building'):version.index_state==='ready'?c('Siap','Ready'):c('Gagal','Failed');
  return <li><div className="version-history-row"><div><h3>{c('Versi ','Version ')+version.version_no}</h3><p><Publication state={version.publication_state} /></p></div>
    <div><p>{c('Ekstraksi teks: ','Text extraction: ')+parse}</p><p>{c('Indeks pengetahuan: ','Knowledge index: ')+index}</p></div>
    <p className="field-hint">{c('Mulai: ','From: ')+(version.valid_from ?? c('Belum dicatat','Not recorded'))}<br />{c('Akhir, eksklusif: ','Until, exclusive: ')+(version.valid_until ?? c('Belum dicatat','Not recorded'))}</p>
    {(version.parse_state==='failed' || version.index_state==='failed') && <p className="version-history-error">{c('Pemeriksaan gagal. Jangan menerbitkan versi ini; periksa file asal atau hubungi pengelola layanan.','Checks failed. Do not publish this version; inspect the original file or contact the service administrator.')}</p>}
  </div></li>;
}

export function KnowledgeDetail({documentId}:{documentId:string}) {
  const c=useCopy();const {api,stamp}=useWorkspace();useSyncExternalStore(api.intents.subscribe,api.intents.getSnapshot);
  const terminal=knowledgeWithdrawn(api,stamp,documentId);const pendingWithdrawal=knowledgePending(api,stamp).some((row) => row.entry.context.documentId===documentId && row.entry.context.kind==='withdraw');
  const decode=useMemo(() => documentDecoder(documentId),[documentId]);const result=useResource('/api/v1/knowledge/documents/'+documentId,decode);
  return <><Link to="/knowledge">{c('Kembali ke dokumen','Back to documents')}</Link><div className="page-heading"><h1>{!terminal && !pendingWithdrawal?result.data?.title ?? c('Detail dokumen','Document details'):c('Dokumen tidak tersedia','Document unavailable')}</h1><p>{c('Status dan versi berasal dari pemeriksaan terbaru. Tanggal berlaku menggunakan waktu Singapura.','Status and versions come from current checks. Effective dates use Singapore time.')}</p></div>
    <PendingKnowledgeOperations documentId={documentId} onSettled={result.refresh} />
    {terminal?<StatusMessage kind="success" title={c('Dokumen telah ditarik','Document withdrawn')}><p>{c('Penarikan bersifat terminal. Gunakan dokumen baru untuk pengganti.','Withdrawal is terminal. Use a new document for its replacement.')}</p></StatusMessage>:!pendingWithdrawal && <ResourceState {...result} retry={result.refresh} />}
    {!terminal && !pendingWithdrawal && result.data && <DocumentDetails key={documentId} document={result.data} refresh={result.refresh} />}
  </>;
}

function DocumentDetails({document,refresh}:{document:KnowledgeDocument;refresh:()=>void}) {
  const c=useCopy();const [cursors,setCursors]=useState<string[]>([]);const after=cursors.at(-1);
  const versions=useResource('/api/v1/knowledge/documents/'+document.document_id+'/versions?limit=20'+(after?'&after='+after:''),decodeVersions);
  const consistent=versions.data?.document_id===document.document_id && versions.data.document_version===document.document_version;
  return <><div className="workspace-detail-grid"><section><h2>{c('Versi aktif','Active version')}</h2>{document.active_version?<><p>{c('Versi ','Version ')+document.active_version.version_no}</p><VersionSummary version={document.active_version} /></>:<p>{c('Belum ada versi yang diterbitkan.','No version has been published.')}</p>}</section>
    <section><h2>{c('Versi terbaru','Latest version')}</h2>{document.latest_version?<><p>{c('Versi ','Version ')+document.latest_version.version_no}</p><VersionSummary version={document.latest_version} />{document.active_version_id && document.latest_version.version_id!==document.active_version_id && <p className="field-hint">{c('Versi aktif tetap digunakan sampai versi pengganti diterbitkan.','The active version remains in use until a replacement is published.')}</p>}</>:<p>{c('Versi belum tersedia.','No version available.')}</p>}</section></div>
    <div className="workspace-actions"><Button onClick={refresh}>{c('Periksa status terbaru','Check current status')}</Button></div>
    {document.latest_version && <VersionProcessing key={document.latest_version.version_id+':'+document.document_version} version={document.latest_version} reload={refresh} />}
    {consistent && versions.data && <KnowledgeLifecycle key={document.document_id+':'+document.document_version+':'+(after ?? '')} document={document} versions={versions.data.items} refresh={refresh} />}
    <DocumentAudience key={document.document_id+':audience:'+document.document_version} documentId={document.document_id} collectionId={document.collection_id} onChanged={refresh} />
    <section><h2>{c('Riwayat versi','Version history')}</h2><ResourceState {...versions} retry={versions.refresh} />
      {versions.data && !consistent && <StatusMessage kind="error" title={c('Dokumen telah berubah','Document has changed')}><p>{c('Muat ulang dokumen untuk memeriksa versi yang konsisten.','Reload the document to inspect consistent versions.')}</p><Button onClick={refresh}>{c('Muat ulang dokumen','Reload document')}</Button></StatusMessage>}
      {consistent && versions.data && <>{versions.data.items.length===0?<p>{c('Belum ada versi.','No versions yet.')}</p>:<ul className="workspace-list">{versions.data.items.map((version) => <VersionHistoryRow key={version.version_id} version={version} />)}</ul>}
        <nav className="workspace-actions" aria-label={c('Halaman versi','Version pages')}><Button disabled={!cursors.length} onClick={() => setCursors(cursors.slice(0,-1))}>{c('Versi sebelumnya','Previous versions')}</Button><Button disabled={!versions.data.next_after} onClick={() => {if (versions.data?.next_after) setCursors([...cursors,versions.data.next_after]);}}>{c('Versi berikutnya','Next versions')}</Button></nav></>}
    </section>
  </>;
}
