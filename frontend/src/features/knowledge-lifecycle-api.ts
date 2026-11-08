import { ApiFailure } from '../api/failure';
import { object } from '../api/transport';
import { boundedText,identifier,isoDate,nullableId,positive } from './knowledge-api';
import type { KnowledgeDocument,KnowledgeVersion } from './knowledge-api';

export interface AdmissionReceipt {document_id:string;document_version:number;version_id:string;version_no:number;parse_state:string;index_state:string;publication_state:string;valid_from:string|null;valid_until:string|null}
export interface KnowledgeOperationContext {kind:'create'|'version'|'publish'|'withdraw';documentId:string|null;collectionId:string|null;versionId?:string;fileId?:string;expectedVersionNo?:number;accessRevision?:number}
export interface LifecycleReceipt {document_id:string;document_version:number;access_revision:number;active_version_id:string|null;withdrawn:boolean;operation:'publish'|'withdraw';operation_version_id:string|null;result_version:number;result_access_revision:number}
export type KnowledgeReceipt={kind:'admission';value:AdmissionReceipt}|{kind:'lifecycle';value:LifecycleReceipt};
export function admissionDecoder(context:KnowledgeOperationContext,body:Record<string,unknown>) {
  const start=isoDate(body.valid_from ?? null);const end=isoDate(body.valid_until ?? null);
  return (value:unknown):AdmissionReceipt => {
    const row=object(value);const document_id=identifier(row.document_id);const document_version=positive(row.document_version);const version_no=positive(row.version_no);
    if (context.documentId && document_id!==context.documentId || !context.documentId && version_no!==1 || context.expectedVersionNo!==undefined && version_no!==context.expectedVersionNo ||
      document_version<(context.kind==='version'?positive(body.expected_version)+1:1) ||
      !['pending','processing','ready','failed'].includes(row.parse_state as string) || !['pending','building','ready','failed'].includes(row.index_state as string) ||
      !['draft','published','superseded','withdrawn'].includes(row.publication_state as string) || isoDate(row.valid_from)!==start || isoDate(row.valid_until)!==end) throw new ApiFailure('malformed');
    return {document_id,document_version,version_id:identifier(row.version_id),version_no,parse_state:row.parse_state as string,index_state:row.index_state as string,publication_state:row.publication_state as string,valid_from:start,valid_until:end};
  };
}
export function lifecycleDecoder(context:KnowledgeOperationContext,body:Record<string,unknown>) {
  const expected=positive(body.expected_version)+1;
  return (value:unknown):LifecycleReceipt => {
    const row=object(value);const document_version=positive(row.document_version);const result_version=positive(row.result_version);const access_revision=positive(row.access_revision);const result_access_revision=positive(row.result_access_revision);
    const active_version_id=nullableId(row.active_version_id);const operation_version_id=nullableId(row.operation_version_id);
    if (row.document_id!==context.documentId || row.operation!==context.kind || result_version!==expected || document_version<result_version || access_revision<result_access_revision ||
      operation_version_id!==(context.kind==='publish'?context.versionId:null) || typeof row.withdrawn!=='boolean' || context.accessRevision!==undefined && result_access_revision!==context.accessRevision+1 ||
      context.kind==='withdraw' && (!row.withdrawn || active_version_id!==null)) throw new ApiFailure('malformed');
    return {document_id:identifier(row.document_id),document_version,result_version,access_revision,result_access_revision,active_version_id,operation_version_id,operation:context.kind as 'publish'|'withdraw',withdrawn:row.withdrawn};
  };
}
export function periodInput(from:string,until:string) {
  const valid_from=from?isoDate(from):null;const valid_until=until?isoDate(until):null;
  if (valid_from && valid_until && valid_until<=valid_from) throw new ApiFailure('input');return {valid_from,valid_until};
}
export function initialAdmissionBody(fileId:string,title:string,period:ReturnType<typeof periodInput>,audienceMode:'specific_people'|'all_current_employees',selectionId:string|null) {
  if (!title.trim() || [...title.trim()].length>200 || [...title].some((char) => char.codePointAt(0)!<32 || char.codePointAt(0)===127)) throw new ApiFailure('input');
  if (audienceMode==='specific_people') identifier(selectionId);else if (selectionId!==null) throw new ApiFailure('input');
  return {file_id:identifier(fileId),title:title.trim(),readers:[],...period,audience_mode:audienceMode,selection_id:selectionId};
}
export interface ExtractedPreview {document_id:string;version_id:string;version_no:number;title:string;access_revision:number;publication_state:string;valid_from:string|null;valid_until:string|null;chunks:{chunk_no:number;page:number;start:number;end:number;text:string;text_digest:string}[]}
export function previewDecoder(document:KnowledgeDocument,version:KnowledgeVersion) {
  return (value:unknown):ExtractedPreview => {
    const row=object(value);
    if (row.preview!==true || row.format!=='extracted_chunks' || row.title!==document.title || row.document_id!==document.document_id || row.version_id!==version.version_id || row.version_no!==version.version_no ||
      row.access_revision!==document.access_revision || row.index_generation!==version.index_generation || row.file_digest!==version.file_digest || row.content_digest!==version.content_digest ||
      row.publication_state!==version.publication_state || isoDate(row.valid_from)!==version.valid_from || isoDate(row.valid_until)!==version.valid_until || !Array.isArray(row.chunks) || row.chunks.length!==version.chunk_count || row.chunks.length<1 || row.chunks.length>32 || version.content_digest===null) throw new ApiFailure('malformed');
    let total=0;let previousPage=0;let previousEnd=0;
    const chunks=row.chunks.map((value,index) => {
      const chunk=object(value);const page=positive(chunk.page);const start=positive(chunk.start,true);const end=positive(chunk.end);const text=boundedText(chunk.text,12288);
      const bytes=new TextEncoder().encode(text).length;total+=bytes;
      if (chunk.chunk_no!==index || page<previousPage || page===previousPage && start<previousEnd || end-start!==[...text].length || bytes>384 || total>12288 || typeof chunk.text_digest!=='string' || !/^[a-f0-9]{64}$/.test(chunk.text_digest)) throw new ApiFailure('malformed');
      previousPage=page;previousEnd=end;return {chunk_no:index,page,start,end,text,text_digest:chunk.text_digest};
    });
    return {document_id:document.document_id,version_id:version.version_id,version_no:version.version_no,title:boundedText(row.title,200),access_revision:document.access_revision,publication_state:version.publication_state,valid_from:version.valid_from,valid_until:version.valid_until,chunks};
  };
}
