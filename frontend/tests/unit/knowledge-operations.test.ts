// @vitest-environment node
import { expect,it } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import { knowledgeExecute,knowledgePending,knowledgeWithdrawn } from '../../src/features/knowledge-operations';
import { fixture,json,tenant } from './api-fixtures';

const did='77777777-7777-4777-8777-777777777777';const vid='88888888-8888-4888-8888-888888888888';const fid='99999999-9999-4999-8999-999999999999';const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
const admission={document_id:did,document_version:1,version_id:vid,version_no:1,parse_state:'pending',index_state:'pending',publication_state:'draft',valid_from:null,valid_until:null};
const lifecycle={document_id:did,document_version:4,access_revision:3,active_version_id:null,withdrawn:true,operation:'withdraw',operation_version_id:null,result_version:4,result_access_revision:3};
it('retains admission key and immutable material across view reentry and explicitly reconciles the same request',async () => {
  let committed=false;const sent:unknown[]=[];const api=new ApiCoordinator(new ApiTransport(async (path,init) => {if (path.endsWith('/me')) return json(fixture());sent.push(init.body);if (!committed) {committed=true;throw new TypeError('lost after commit fixture');}return json(admission,202,binding);}),{request:async (_name,_options,work) => work()});const stamp=await api.bootstrap();
  const body={file_id:fid,title:'Policy',readers:[],valid_from:null,valid_until:null,audience_mode:'all_current_employees',selection_id:null};
  await expect(knowledgeExecute(api,stamp,{method:'POST',path:'/api/v1/knowledge/documents',body},{kind:'create',documentId:null,collectionId:null,fileId:fid})).rejects.toMatchObject({outcome:'unknown'});
  const pending=knowledgePending(api,stamp)[0];expect(pending.entry.context.fileId).toBe(fid);
  await expect(knowledgeExecute(api,stamp,{method:'POST',path:'/api/v1/knowledge/documents',body},{kind:'create',documentId:null,collectionId:null,fileId:fid})).rejects.toMatchObject({kind:'conflict'});
  expect((await knowledgeExecute(api,stamp,undefined,undefined,pending.entry.key)).value.document_id).toBe(did);expect(sent).toHaveLength(2);expect(sent[0]).toBe(sent[1]);api.dispose();
});
it('preserves unknown exact intent after wrong-document success and masks terminal before settlement notification',async () => {
  let correct=false;const api=new ApiCoordinator(new ApiTransport(async path => path.endsWith('/me')?json(fixture()):json({...lifecycle,document_id:correct?did:tenant},200,binding)),{request:async (_name,_options,work) => work()});const stamp=await api.bootstrap();
  await expect(knowledgeExecute(api,stamp,{method:'POST',path:'/api/v1/knowledge/documents/'+did+'/withdraw',body:{expected_version:3}},{kind:'withdraw',documentId:did,collectionId:null,accessRevision:2})).rejects.toMatchObject({outcome:'unknown'});
  expect(knowledgeWithdrawn(api,stamp,did)).toBe(false);const pending=knowledgePending(api,stamp)[0];correct=true;const observed:boolean[]=[];
  const unsub=api.intents.subscribe(() => {if (!knowledgePending(api,stamp).length) observed.push(knowledgeWithdrawn(api,stamp,did));});
  await knowledgeExecute(api,stamp,undefined,undefined,pending.entry.key);expect(observed.length).toBeGreaterThan(0);expect(observed.every(Boolean)).toBe(true);expect(knowledgeWithdrawn(api,stamp,did)).toBe(true);unsub();api.invalidate();expect(knowledgeWithdrawn(api,stamp,did)).toBe(false);api.dispose();
});
it('drops pending private bindings on context invalidation and rejects cross-resource context before dispatch',async () => {
  const api=new ApiCoordinator(new ApiTransport(async path => path.endsWith('/me')?json(fixture()):Promise.reject(new TypeError('controlled lost reply'))),{request:async (_name,_options,work) => work()});const stamp=await api.bootstrap();
  await expect(knowledgeExecute(api,stamp,{method:'POST',path:'/api/v1/knowledge/documents/'+tenant+'/withdraw',body:{expected_version:3}},{kind:'withdraw',documentId:did,collectionId:null})).rejects.toMatchObject({kind:'input'});
  await expect(knowledgeExecute(api,stamp,{method:'POST',path:'/api/v1/knowledge/documents/'+did+'/withdraw',body:{expected_version:3}},{kind:'withdraw',documentId:did,collectionId:null})).rejects.toMatchObject({outcome:'unknown'});expect(knowledgePending(api,stamp)).toHaveLength(1);api.invalidate();expect(knowledgePending(api,stamp)).toHaveLength(0);api.dispose();
});
