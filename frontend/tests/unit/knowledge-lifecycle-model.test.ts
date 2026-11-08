// @vitest-environment node
import { expect,it } from 'vitest';
import { admissionDecoder,initialAdmissionBody,lifecycleDecoder,periodInput,previewDecoder } from '../../src/features/knowledge-lifecycle-api';
import { decodeDocument } from '../../src/features/knowledge-api';

const did='77777777-7777-4777-8777-777777777777';const vid='88888888-8888-4888-8888-888888888888';const fid='99999999-9999-4999-8999-999999999999';
const status={document_id:did,document_version:3,version_id:vid,version_no:1,parse_state:'ready',index_state:'ready',publication_state:'draft',valid_from:null,valid_until:null};
const version={...status,chunk_count:1,file_id:fid,file_digest:'a'.repeat(64),content_digest:'b'.repeat(64),index_generation:1,created_at:'2026-11-05T00:00:00Z',error_code:null};
const document=decodeDocument({document_id:did,title:'Policy',document_version:3,access_revision:2,collection_id:null,active_version_id:null,active_version:null,latest_version:version,created_at:version.created_at});
const preview={document_id:did,title:'Policy',version_id:vid,version_no:1,publication_state:'draft',access_revision:2,index_generation:1,file_digest:'a'.repeat(64),content_digest:'b'.repeat(64),preview:true,format:'extracted_chunks',valid_from:null,valid_until:null,chunks:[{chunk_no:0,page:1,start:0,end:6,text:'Policy',text_digest:'c'.repeat(64)}]};
it('validates canonical version-local dates and explicit initial audience fields without conflating empty readers with all',() => {
  expect(periodInput('','')).toEqual({valid_from:null,valid_until:null});expect(() => periodInput('2026-02-30','')).toThrow();expect(() => periodInput('2026-11-05','2026-11-05')).toThrow();
  expect(initialAdmissionBody(fid,' Full title ',periodInput('',''),'specific_people',vid)).toMatchObject({title:'Full title',readers:[],audience_mode:'specific_people',selection_id:vid});
  expect(initialAdmissionBody(fid,'Policy',periodInput('',''),'all_current_employees',null).selection_id).toBeNull();expect(() => initialAdmissionBody(fid,'',periodInput('',''),'all_current_employees',null)).toThrow();expect(() => initialAdmissionBody(fid,'bad\u0000title',periodInput('',''),'all_current_employees',null)).toThrow();
});
it('binds admission receipts to all request fields supplied by the receipt contract and permits later current states on replay',() => {
  const decode=admissionDecoder({kind:'version',documentId:did,collectionId:null,fileId:fid,expectedVersionNo:2},{file_id:fid,expected_version:2,valid_from:null,valid_until:null});
  expect(decode({...status,document_version:8,version_no:2,publication_state:'superseded'}).document_version).toBe(8);
  for (const change of [{document_id:fid},{document_version:2},{version_no:3},{valid_from:'2026-11-05'},{parse_state:'done'}]) expect(() => decode({...status,version_no:2,...change})).toThrow();
});
it('validates lifecycle receipt operation and original CAS independently from current state after subsequent writes',() => {
  const decode=lifecycleDecoder({kind:'publish',documentId:did,collectionId:null,versionId:vid,accessRevision:2},{expected_version:3});
  const receipt={document_id:did,document_version:8,access_revision:7,active_version_id:fid,withdrawn:false,operation:'publish',operation_version_id:vid,result_version:4,result_access_revision:3};
  expect(decode(receipt).active_version_id).toBe(fid);
  for (const change of [{document_id:fid},{operation:'withdraw'},{operation_version_id:fid},{result_version:5},{result_access_revision:4}]) expect(() => decode({...receipt,...change})).toThrow();
  const withdraw=lifecycleDecoder({kind:'withdraw',documentId:did,collectionId:null,accessRevision:2},{expected_version:3});
  expect(() => withdraw({...receipt,operation:'withdraw',operation_version_id:null,withdrawn:false})).toThrow();
});
it('checks exact extracted version/generation/digest/period and ordered code-point chunk locations without HTML rendering',() => {
  const decode=previewDecoder(document,document.latest_version!);expect(decode(preview).chunks[0].text).toBe('Policy');
  for (const change of [{version_id:fid},{index_generation:2},{access_revision:3},{file_digest:'c'.repeat(64)},{preview:false},{valid_until:'2026-11-07'},{chunks:[{...preview.chunks[0],end:7}]}]) expect(() => decode({...preview,...change})).toThrow();
});
