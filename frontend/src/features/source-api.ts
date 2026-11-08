import { ApiFailure } from '../api/failure';
import { object } from '../api/transport';
import { boundedText,identifier,isoDate,positive } from './knowledge-api';
import type { TextRange } from '../components/SourceHighlight';

function digest(value:unknown):string {if (typeof value!=='string' || !/^[a-f0-9]{64}$/.test(value)) throw new ApiFailure('malformed');return value;}
export interface SourceChunk {chunk_no:number;page:number;start:number;end:number;text:string;text_digest:string}
export interface SearchHit extends SourceChunk {document_id:string;title:string;version_id:string;version_no:number;access_revision:number;index_generation:number;content_digest:string;publication_state:'published'}
export interface SourceContent {document_id:string;title:string;version_id:string;version_no:number;access_revision:number;index_generation:number;content_digest:string;publication_state:'published'|'superseded';valid_from:string|null;valid_until:string|null;chunks:SourceChunk[]}
// A citation binds a version and exact chunk, independently of its list position.
export interface SourceReference {version_id:string;document_id?:string;version_no?:number;index_generation?:number;content_digest?:string;chunk_no?:number;page?:number;start?:number;end?:number;text_digest?:string;quote?:string;label?:string}
function chunk(value:unknown):SourceChunk {
  const row=object(value);const text=boundedText(row.text,384);const start=positive(row.start,true);const end=positive(row.end);
  if (new TextEncoder().encode(text).length>384 || end-start!==[...text].length) throw new ApiFailure('malformed');
  return {chunk_no:positive(row.chunk_no,true),page:positive(row.page),start,end,text,text_digest:digest(row.text_digest)};
}
export function decodeSearch(value:unknown):{items:SearchHit[]} {
  const row=object(value);if (row.search_mode!=='lexical_simple_v1' || !Array.isArray(row.items) || row.items.length>10) throw new ApiFailure('malformed');
  const items=row.items.map((value):SearchHit => {const hit=object(value);if (hit.publication_state!=='published') throw new ApiFailure('malformed');return {...chunk(hit),document_id:identifier(hit.document_id),title:boundedText(hit.title,200),version_id:identifier(hit.version_id),version_no:positive(hit.version_no),access_revision:positive(hit.access_revision),index_generation:positive(hit.index_generation),content_digest:digest(hit.content_digest),publication_state:'published'};});
  if (new Set(items.map((hit) => `${hit.version_id}:${hit.chunk_no}`)).size!==items.length) throw new ApiFailure('malformed');return {items};
}
export function sourceDecoder(versionId:string):(value:unknown)=>SourceContent {
  identifier(versionId);return (value) => {
    const row=object(value);if (row.version_id!==versionId || row.format!=='extracted_chunks' || !['published','superseded'].includes(String(row.publication_state)) || !Array.isArray(row.chunks) || row.chunks.length<1 || row.chunks.length>32) throw new ApiFailure('malformed');
    const chunks=row.chunks.map(chunk);const valid_from=isoDate(row.valid_from);const valid_until=isoDate(row.valid_until);
    if (valid_from && valid_until && valid_from>=valid_until || chunks.some((item,index) => index>0 && (item.chunk_no<=chunks[index-1].chunk_no || item.page<chunks[index-1].page || item.page===chunks[index-1].page && item.start<chunks[index-1].end)) || chunks.reduce((total,item) => total+new TextEncoder().encode(item.text).length,0)>12288) throw new ApiFailure('malformed');
    digest(row.file_digest);boundedText(row.embedding_model,200);boundedText(row.chunk_config,200);boundedText(row.parser_ref,200);
    return {document_id:identifier(row.document_id),title:boundedText(row.title,200),version_id:versionId,version_no:positive(row.version_no),access_revision:positive(row.access_revision),index_generation:positive(row.index_generation),content_digest:digest(row.content_digest),publication_state:row.publication_state as SourceContent['publication_state'],valid_from,valid_until,chunks};
  };
}
export function searchPath(query:string,language:'id'|'en'):string {
  const q=query.trim();if (!q || [...q].length>200 || new TextEncoder().encode(q).length>800 || [...query].some((character) => {const code=character.codePointAt(0)!;return code<32 || code>=127 && code<=159;})) throw new ApiFailure('input');
  return '/api/v1/knowledge/search?'+new URLSearchParams({q,language,limit:'5'}).toString();
}
export function sourceLocation(source:SourceContent,reference:SourceReference):{chunk:SourceChunk;ranges:TextRange[]}|undefined {
  if (source.version_id!==reference.version_id || reference.document_id!==undefined && source.document_id!==reference.document_id || reference.version_no!==undefined && source.version_no!==reference.version_no || reference.index_generation!==undefined && source.index_generation!==reference.index_generation || reference.content_digest!==undefined && source.content_digest!==reference.content_digest) throw new ApiFailure('not_found');
  if (reference.chunk_no===undefined) return undefined;
  const target=source.chunks.find((item) => item.chunk_no===reference.chunk_no);
  if (!target || reference.page!==undefined && target.page!==reference.page || reference.text_digest!==undefined && target.text_digest!==reference.text_digest || reference.start!==undefined && target.start!==reference.start || reference.end!==undefined && target.end!==reference.end) throw new ApiFailure('not_found');
  if (reference.quote!==undefined) {
    const start=target.text.indexOf(reference.quote);
    if (!reference.quote || start<0) throw new ApiFailure('not_found');
    // Repeated identical quotes have no unique sublocation; mark the exact bound chunk.
    if (target.text.indexOf(reference.quote,start+1)<0) return {chunk:target,ranges:[{start,end:start+reference.quote.length}]};
  }
  return {chunk:target,ranges:[{start:0,end:target.text.length}]};
}
