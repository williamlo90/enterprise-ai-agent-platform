import { ApiFailure } from '../api/failure';
import { maximumUploadBytes } from '../api/upload-policy';
import type { UploadMime } from '../api/upload-policy';
import { object } from '../api/transport';
import { identifier,positive } from './knowledge-api';

// The extension makes Markdown explicit even when the OS reports text/plain.
// This is only client guidance; bytes, encoding and parser readiness are server decisions.
export function fileMime(file:Pick<File,'name'|'type'|'size'>):UploadMime {
  const extension=file.name.split('.').at(-1)?.toLowerCase();
  const mime=extension==='pdf'?'application/pdf':extension==='txt'?'text/plain':extension==='md' || extension==='markdown'?'text/markdown':undefined;
  if (!mime || file.size<1 || file.size>maximumUploadBytes ||
      file.type && file.type!=='application/octet-stream' && file.type!==mime && !(mime==='text/markdown' && file.type==='text/plain')) throw new ApiFailure('input');
  return mime;
}
export interface KnowledgeFile {id:string;purpose:'knowledge';mime:UploadMime;size_bytes:number;scan_state:'quarantine'|'ready'|'rejected';version:number}
export function fileDecoder(expected:{mime:UploadMime;size:number;id?:string;version?:number}) {
  return (value:unknown):KnowledgeFile => {
    const row=object(value);const id=identifier(row.id);const version=positive(row.version);
    if (expected.id && id!==expected.id || row.purpose!=='knowledge' || row.mime!==expected.mime || row.size_bytes!==expected.size ||
        expected.version && version<expected.version || !['quarantine','ready','rejected'].includes(row.scan_state as string)) throw new ApiFailure('malformed');
    return {id,purpose:'knowledge',mime:expected.mime,size_bytes:expected.size,scan_state:row.scan_state as KnowledgeFile['scan_state'],version};
  };
}
export interface ProcessingStatus {
  document_id:string;version_id:string;document_version:number;version_no:number;
  parse_state:'pending'|'processing'|'ready'|'failed';index_state:'pending'|'building'|'ready'|'failed';
  publication_state:'draft'|'published'|'superseded';
}
export function processingDecoder(documentId:string,versionId:string,versionNo:number) {
  return (value:unknown):ProcessingStatus => {
    const row=object(value);
    if (row.document_id!==documentId || row.version_id!==versionId || row.version_no!==versionNo ||
        !['pending','processing','ready','failed'].includes(row.parse_state as string) ||
        !['pending','building','ready','failed'].includes(row.index_state as string) ||
        !['draft','published','superseded'].includes(row.publication_state as string)) throw new ApiFailure('malformed');
    return {document_id:documentId,version_id:versionId,document_version:positive(row.document_version),version_no:versionNo,
      parse_state:row.parse_state as ProcessingStatus['parse_state'],index_state:row.index_state as ProcessingStatus['index_state'],publication_state:row.publication_state as ProcessingStatus['publication_state']};
  };
}
