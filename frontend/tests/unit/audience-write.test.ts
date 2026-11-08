// @vitest-environment node
import { expect,it } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import { audienceReceiptDecoder,selectionReceiptDecoder } from '../../src/features/audience-api';
import { fixture,json,tenant } from './api-fixtures';

const did='77777777-7777-4777-8777-777777777777';const sid='88888888-8888-4888-8888-888888888888';const key='99999999-9999-4999-8999-999999999999';const binding={'X-EAI-Context-Binding':'a'.repeat(64)};
it('preserves the exact original apply intent when success metadata mismatches and reconciles without a new key',async () => {
  const body={audience_mode:'all_current_employees',selection_id:null,expected_version:3};
  for (const changed of [{document_id:tenant},{audience_mode:'specific_people'},{document_version:8},{selection_id:sid},{explicit_readers_count:1}]) {
    let valid=false;const sent:RequestInit[]=[];
    const api=new ApiCoordinator(new ApiTransport(async (path,init) => {if (path.endsWith('/me')) return json(fixture());sent.push(init);return json({document_id:did,document_version:4,access_revision:4,audience_mode:'all_current_employees',explicit_readers_count:0,selection_id:null,...(valid?{}:changed)},200,binding);}),{request:async (_name,_options,work) => work()});const stamp=await api.bootstrap();
    const intent=api.prepareWrite({method:'PATCH',path:'/api/v1/knowledge/documents/'+did+'/audience',body},stamp,key);
    await expect(api.dispatchWrite(key,stamp,audienceReceiptDecoder(did,body))).rejects.toMatchObject({outcome:'unknown'});
    expect(api.intents.current(key)).toMatchObject({key,material:intent.material,state:'unknown'});
    valid=true;await api.dispatchWrite(key,stamp,audienceReceiptDecoder(did,body),{reconcile:true});expect(sent[0].body).toBe(sent[1].body);expect(api.intents.current(key)).toBeUndefined();api.dispose();
  }
});
it('preserves unknown selection creation when success has a foreign scope or base binding',async () => {
  const body={collection_id:null,base_document_id:did,expected_version:3,retain_existing:true,all_matching:true,filters:{},include_ids:[],exclude_ids:[]};
  const api=new ApiCoordinator(new ApiTransport(async path => path.endsWith('/me')?json(fixture()):json({selection_id:sid,selected_count:0,snapshot_digest:'a'.repeat(64),expires_at:1900000000,creator_grant_included:false,base_document_id:tenant,expected_document_version:3,collection_id:null,retain_existing:true},201,binding)),{request:async (_name,_options,work) => work()});const stamp=await api.bootstrap();
  api.prepareWrite({method:'POST',path:'/api/v1/knowledge/selections',body},stamp,key);
  await expect(api.dispatchWrite(key,stamp,selectionReceiptDecoder(body))).rejects.toMatchObject({outcome:'unknown'});expect(api.intents.current(key)?.state).toBe('unknown');api.dispose();
});
