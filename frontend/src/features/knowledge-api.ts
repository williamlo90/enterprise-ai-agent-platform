import { ApiFailure } from '../api/failure';
import { isUuid,object } from '../api/transport';

export function identifier(value:unknown):string {if (!isUuid(value)) throw new ApiFailure('malformed');return value;}
export function positive(value:unknown,zero=false):number {if (!Number.isSafeInteger(value) || (value as number)<(zero?0:1)) throw new ApiFailure('malformed');return value as number;}
export function boundedText(value:unknown,maximum:number):string {if (typeof value!=='string' || !value.trim() || [...value].length>maximum) throw new ApiFailure('malformed');return value;}
export function nullableId(value:unknown):string|null {return value===null?null:identifier(value);}
export function isoDate(value:unknown):string|null {
  if (value===null) return null;
  if (typeof value!=='string' || !/^(?!0000)[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)) throw new ApiFailure('malformed');
  const date=new Date(value+'T00:00:00Z');if (!Number.isFinite(date.valueOf()) || date.toISOString().slice(0,10)!==value) throw new ApiFailure('malformed');return value;
}
function timestamp(value:unknown):string {if (typeof value!=='string' || value.length>64 || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}T/.test(value) || !Number.isFinite(Date.parse(value))) throw new ApiFailure('malformed');return value;}
function choice<T extends string>(value:unknown,options:readonly T[]):T {if (typeof value!=='string' || !options.includes(value as T)) throw new ApiFailure('malformed');return value as T;}
function digest(value:unknown):string {if (typeof value!=='string' || !/^[a-f0-9]{64}$/.test(value)) throw new ApiFailure('malformed');return value;}
export interface KnowledgeVersion {
  document_id:string;document_version:number;version_id:string;version_no:number;
  parse_state:'pending'|'processing'|'ready'|'failed';index_state:'pending'|'building'|'ready'|'failed';
  publication_state:'draft'|'published'|'superseded'|'withdrawn';chunk_count:number|null;
  file_id:string;file_digest:string;content_digest:string|null;index_generation:number;
  created_at:string;valid_from:string|null;valid_until:string|null;error_code:string|null;
}
export function decodeVersion(value:unknown):KnowledgeVersion {
  const row=object(value);const valid_from=isoDate(row.valid_from);const valid_until=isoDate(row.valid_until);
  if (valid_from && valid_until && valid_from>=valid_until) throw new ApiFailure('malformed');
  return {document_id:identifier(row.document_id),document_version:positive(row.document_version),version_id:identifier(row.version_id),version_no:positive(row.version_no),
    parse_state:choice(row.parse_state,['pending','processing','ready','failed']),index_state:choice(row.index_state,['pending','building','ready','failed']),publication_state:choice(row.publication_state,['draft','published','superseded','withdrawn']),
    chunk_count:row.chunk_count===null?null:positive(row.chunk_count,true),file_id:identifier(row.file_id),file_digest:digest(row.file_digest),content_digest:row.content_digest===null?null:digest(row.content_digest),index_generation:positive(row.index_generation,true),created_at:timestamp(row.created_at),valid_from,valid_until,error_code:row.error_code===null?null:boundedText(row.error_code,100)};
}
export interface KnowledgeDocument {document_id:string;title:string;collection_id:string|null;document_version:number;access_revision:number;active_version_id:string|null;created_at:string;active_version:KnowledgeVersion|null;latest_version:KnowledgeVersion|null}
export function decodeDocument(value:unknown):KnowledgeDocument {
  const row=object(value);const document_id=identifier(row.document_id);const document_version=positive(row.document_version);const active_version_id=nullableId(row.active_version_id);
  const active_version=row.active_version===null?null:decodeVersion(row.active_version);const latest_version=row.latest_version===null?null:decodeVersion(row.latest_version);
  if ((active_version_id===null)!==(active_version===null) || active_version && (active_version.version_id!==active_version_id || active_version.publication_state!=='published') || [active_version,latest_version].some((version) => version && (version.document_id!==document_id || version.document_version!==document_version)) || active_version && latest_version && active_version.version_no>latest_version.version_no) throw new ApiFailure('malformed');
  return {document_id,title:boundedText(row.title,200),collection_id:nullableId(row.collection_id),document_version,access_revision:positive(row.access_revision),active_version_id,created_at:timestamp(row.created_at),active_version,latest_version};
}
export function documentDecoder(expectedId:string):(value:unknown)=>KnowledgeDocument {
  identifier(expectedId);
  return (value) => {const document=decodeDocument(value);if (document.document_id!==expectedId) throw new ApiFailure('malformed');return document;};
}
export function decodeCatalog(value:unknown):{items:KnowledgeDocument[];next_after:string|null} {
  const row=object(value);if (!Array.isArray(row.items) || row.items.length>50) throw new ApiFailure('malformed');const items=row.items.map(decodeDocument);
  if (new Set(items.map((item) => item.document_id)).size!==items.length) throw new ApiFailure('malformed');
  return {items,next_after:nullableId(row.next_after)};
}
export function decodeVersions(value:unknown):{document_id:string;document_version:number;items:KnowledgeVersion[];next_after:string|null} {
  const row=object(value);const document_id=identifier(row.document_id);const document_version=positive(row.document_version);
  if (!Array.isArray(row.items) || row.items.length>50) throw new ApiFailure('malformed');const items=row.items.map(decodeVersion);
  if (items.some((item,index) => item.document_id!==document_id || item.document_version!==document_version || index>0 && item.version_no>=items[index-1].version_no) || new Set(items.map((item) => item.version_id)).size!==items.length) throw new ApiFailure('malformed');
  return {document_id,document_version,items,next_after:nullableId(row.next_after)};
}
export function decodeCollections(value:unknown):{tenant_level_allowed:boolean;items:{collection_id:string;display_name:string|null}[];next_after:string|null} {
  const row=object(value);if (typeof row.tenant_level_allowed!=='boolean' || !Array.isArray(row.items) || row.items.length>50) throw new ApiFailure('malformed');
  const items=row.items.map((value) => {const item=object(value);return {collection_id:identifier(item.collection_id),display_name:item.display_name===null?null:boundedText(item.display_name,200)};});
  if (new Set(items.map((item) => item.collection_id)).size!==items.length) throw new ApiFailure('malformed');
  return {tenant_level_allowed:row.tenant_level_allowed,items,next_after:nullableId(row.next_after)};
}
