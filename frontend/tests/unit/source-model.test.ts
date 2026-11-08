import { expect,it } from 'vitest';
import { decodeSearch,searchPath,sourceDecoder,sourceLocation } from '../../src/features/source-api';
import { canOpen } from '../../src/app/auth-controller';
import { fixture,tenant } from './api-fixtures';

import { versionId,sourceChunk,sourceHit,sourceValue } from './source-fixtures';

it('enforces the manual lexical Unicode/byte/control query budget and encodes literal query text',() => {
  const path=searchPath(' biaya & perjalanan ','id');expect(new URL(path,'https://example.test').searchParams.get('q')).toBe('biaya & perjalanan');
  expect(() => searchPath('😀'.repeat(200),'en')).not.toThrow();expect(() => searchPath('😀'.repeat(201),'id')).toThrow();
  for (const query of ['', ' ', 'biaya\nperjalanan','a\u007fb']) expect(() => searchPath(query,'id')).toThrow();
});
it('rejects draft, duplicate, malformed and oversized search results',() => {
  expect(decodeSearch({items:[sourceHit],search_mode:'lexical_simple_v1'}).items).toHaveLength(1);
  for (const items of [[{...sourceHit,publication_state:'draft'}],[sourceHit,sourceHit],[{...sourceHit,end:99}],[{...sourceHit,text:'😀'.repeat(100),end:100}]]) expect(() => decodeSearch({items,search_mode:'lexical_simple_v1'})).toThrow();
  expect(() => decodeSearch({items:[],search_mode:'semantic'})).toThrow();
});
it('decodes superseded sources and binds the exact version, chunk, page, generation and digest',() => {
  const source=sourceDecoder(versionId)({...sourceValue,publication_state:'superseded'});expect(source.publication_state).toBe('superseded');
  expect(sourceLocation(source,sourceHit)?.chunk.chunk_no).toBe(0);
  for (const reference of [{...sourceHit,version_id:tenant},{...sourceHit,chunk_no:1},{...sourceHit,page:2},{...sourceHit,text_digest:'e'.repeat(64)},{...sourceHit,index_generation:2},{...sourceHit,content_digest:'e'.repeat(64)},{...sourceHit,start:1}]) expect(() => sourceLocation(source,reference)).toThrow();
  expect(() => sourceDecoder(tenant)(sourceValue)).toThrow();expect(() => sourceDecoder(versionId)({...sourceValue,publication_state:'withdrawn'})).toThrow();
});
it('maps exact citation quotes to UTF16 spans after astral characters and never matches a different chunk',() => {
  const source=sourceDecoder(versionId)(sourceValue);const reference={version_id:versionId,chunk_no:0,page:1,text_digest:sourceChunk.text_digest,quote:'Biaya'};
  expect(sourceLocation(source,reference)?.ranges).toEqual([{start:3,end:8}]);expect(() => sourceLocation(source,{...reference,quote:'foreign quote'})).toThrow();
});
it('accepts page-local offsets restarting at zero on page two and highlights that exact page',() => {
  const pageTwo={...sourceChunk,chunk_no:1,page:2};const source=sourceDecoder(versionId)({...sourceValue,chunks:[sourceChunk,pageTwo]});
  expect(source.chunks).toHaveLength(2);expect(sourceLocation(source,{version_id:versionId,chunk_no:1,page:2,start:0,end:18,text_digest:pageTwo.text_digest})?.chunk).toEqual(pageTwo);
});
it.each([
  {name:'overlapping offsets on the same page',chunks:[sourceChunk,{...sourceChunk,chunk_no:1}]},
  {name:'a backwards page number',chunks:[{...sourceChunk,page:2},{...sourceChunk,chunk_no:1,page:1,start:18,end:36}]},
])('rejects $name',({chunks}) => {
  expect(() => sourceDecoder(versionId)({...sourceValue,chunks})).toThrow();
});
it('allows reader route attempts for any active tenant while preserving manager-only discovery',() => {
  const identity=fixture();identity.memberships[0].roles=[];expect(canOpen(identity,'/search')).toBe(true);expect(canOpen(identity,'/sources/'+versionId)).toBe(true);expect(canOpen(identity,'/knowledge')).toBe(false);
  expect(canOpen(identity,'/sources/'+versionId+'/extra')).toBe(false);
});
