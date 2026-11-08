import { ApiFailure } from '../api/failure';
import { object } from '../api/transport';
import { boundedText,identifier,nullableId,positive } from './knowledge-api';

export type AudienceMode='specific_people'|'all_current_employees';
export interface Reader {principal_id:string;membership_id:string|null;display_name:string|null;membership_active:boolean;principal_active:boolean;eligible:boolean}
export function decodeReader(value:unknown):Reader {
  const row=object(value);
  if (typeof row.membership_active!=='boolean' || typeof row.principal_active!=='boolean') throw new ApiFailure('malformed');
  const eligible=row.membership_active && row.principal_active;
  if (row.eligible!==undefined && row.eligible!==eligible) throw new ApiFailure('malformed');
  return {principal_id:identifier(row.principal_id),membership_id:nullableId(row.membership_id),display_name:row.display_name===null?null:boundedText(row.display_name,200),membership_active:row.membership_active,principal_active:row.principal_active,eligible};
}
export function mode(value:unknown):AudienceMode {if (value!=='specific_people' && value!=='all_current_employees') throw new ApiFailure('malformed');return value;}
function readers(value:unknown,ordered=true):Reader[] {
  if (!Array.isArray(value) || value.length>50) throw new ApiFailure('malformed');
  const rows=value.map(decodeReader);if (new Set(rows.map((row) => row.principal_id)).size!==rows.length) throw new ApiFailure('malformed');if (ordered && rows.some((row,index) => index>0 && row.principal_id<=rows[index-1].principal_id)) throw new ApiFailure('malformed');return rows;
}
export interface Audience {document_id:string;document_version:number;access_revision:number;audience_mode:AudienceMode;explicit_readers_count:number;readers:Reader[];next_after:string|null}
export function audienceDecoder(documentId:string) {
  return (value:unknown):Audience => {
    const row=object(value);if (row.document_id!==documentId || !Array.isArray(row.other_authorities) || row.other_authorities.length!==1 || row.other_authorities[0]!=='knowledge_manager' || row.dynamic_employee_role!==(row.audience_mode==='all_current_employees')) throw new ApiFailure('malformed');
    const audience_mode=mode(row.audience_mode);const count=positive(row.explicit_readers_count,true);const items=readers(row.readers);const next_after=nullableId(row.next_after);
    if (items.length>count || audience_mode==='all_current_employees' && count!==0 || next_after && next_after!==items.at(-1)?.principal_id) throw new ApiFailure('malformed');
    return {document_id:documentId,document_version:positive(row.document_version),access_revision:positive(row.access_revision),audience_mode,explicit_readers_count:count,readers:items,next_after};
  };
}
export interface FrozenSelection {selection_id:string;selected_count:number;snapshot_digest:string;expires_at:number;creator_grant_included:boolean;base_document_id:string|null;expected_document_version:number|null;collection_id:string|null;retain_existing:boolean}
export function decodeSelection(value:unknown):FrozenSelection {
  const row=object(value);
  if (typeof row.snapshot_digest!=='string' || !/^[a-f0-9]{64}$/.test(row.snapshot_digest) || typeof row.expires_at!=='number' || !Number.isFinite(row.expires_at) || row.expires_at<=0 || typeof row.creator_grant_included!=='boolean' || typeof row.retain_existing!=='boolean') throw new ApiFailure('malformed');
  const count=positive(row.selected_count,true);if (count>1000) throw new ApiFailure('malformed');
  const base=nullableId(row.base_document_id);const version=row.expected_document_version===null?null:positive(row.expected_document_version);
  if ((base===null)!==(version===null)) throw new ApiFailure('malformed');
  return {selection_id:identifier(row.selection_id),selected_count:count,snapshot_digest:row.snapshot_digest,expires_at:row.expires_at,creator_grant_included:row.creator_grant_included,base_document_id:base,expected_document_version:version,collection_id:nullableId(row.collection_id),retain_existing:row.retain_existing};
}
export function decodeSelectionPage(value:unknown) {
  const row=object(value);const selection=decodeSelection(row);const items=readers(row.items);const next_after=nullableId(row.next_after);
  if (typeof row.consumed!=='boolean' || row.consumed || items.length>selection.selected_count || next_after && next_after!==items.at(-1)?.principal_id) throw new ApiFailure('malformed');
  return {...selection,items,next_after};
}
export function selectionReceiptDecoder(body:Record<string,unknown>) {
  return (value:unknown):FrozenSelection => {
    const receipt=decodeSelection(value);
    if (receipt.base_document_id!==body.base_document_id || receipt.collection_id!==body.collection_id || receipt.expected_document_version!==body.expected_version || receipt.retain_existing!==body.retain_existing) throw new ApiFailure('malformed');
    return receipt;
  };
}
export function decodeDirectory(value:unknown) {
  const row=object(value);const items=readers(row.items,false);if (items.some((item) => !item.eligible || !item.membership_id)) throw new ApiFailure('malformed');
  if (items.some((row,index) => index>0 && row.membership_id!<=items[index-1].membership_id!)) throw new ApiFailure('malformed');
  const next_after=nullableId(row.next_after);if (next_after && next_after!==items.at(-1)?.membership_id) throw new ApiFailure('malformed');
  return {items,next_after};
}
export function decodeAudienceReceipt(value:unknown) {
  const row=object(value);return {document_id:identifier(row.document_id),document_version:positive(row.document_version),access_revision:positive(row.access_revision),audience_mode:mode(row.audience_mode),explicit_readers_count:positive(row.explicit_readers_count,true),selection_id:nullableId(row.selection_id)};
}
export function audienceReceiptDecoder(documentId:string,body:Record<string,unknown>) {
  const expectedMode=mode(body.audience_mode);const expectedVersion=positive(body.expected_version);const selectionId=nullableId(body.selection_id);
  return (value:unknown) => {
    const receipt=decodeAudienceReceipt(value);
    if (receipt.document_id!==documentId || receipt.audience_mode!==expectedMode || receipt.selection_id!==selectionId || receipt.document_version!==expectedVersion+1 || expectedMode==='all_current_employees' && receipt.explicit_readers_count!==0) throw new ApiFailure('malformed');
    return receipt;
  };
}
export interface ReaderFilters {q:string;department:string;job_title:string;group:string}
export const emptyReaderFilters:ReaderFilters={q:'',department:'',job_title:'',group:''};
export function directoryPath(collectionId:string|null,filters:ReaderFilters,after?:string) {
  const params=new URLSearchParams({limit:'50'});if (collectionId) params.set('collection',identifier(collectionId));
  for (const [key,value] of Object.entries(filters)) if (value.trim()) params.set(key,value.trim());
  if (after) params.set('after',identifier(after));return '/api/v1/knowledge/directory?'+params.toString();
}
// Only eligible additions are sent explicitly. Inactive existing grants stay on
// the server through retain_existing, never disappear with directory pagination.
export function selectionBody(collectionId:string|null,base:Audience|null,selected:Reader[],excluded:Set<string>,filters:ReaderFilters,allMatching:boolean) {
  const baseIds=new Set(base?.readers.map((row) => row.principal_id));
  const retain=!!base && selected.some((row) => baseIds.has(row.principal_id));
  const include=[...new Set(selected.filter((row) => row.eligible && (!retain || !baseIds.has(row.principal_id))).map((row) => row.principal_id))].sort();
  const exclude=[...new Set([...excluded,...(retain?base!.readers.filter((row) => !selected.some((item) => item.principal_id===row.principal_id)).map((row) => row.principal_id):[])])].sort();
  const body={collection_id:collectionId,base_document_id:base?.document_id ?? null,expected_version:base?.document_version ?? null,retain_existing:retain,all_matching:allMatching,filters:Object.fromEntries(Object.entries(filters).map(([key,value]) => [key,value.trim() || null])),include_ids:include,exclude_ids:exclude};
  if (selected.length>1000 || include.length>1000 || exclude.length>1000 || new TextEncoder().encode(JSON.stringify({...body,key:'0'.repeat(36)})).length>65536) throw new ApiFailure('input');
  return body;
}
