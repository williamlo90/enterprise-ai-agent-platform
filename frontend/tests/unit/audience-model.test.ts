// @vitest-environment node
import { expect,it } from 'vitest';
import { audienceDecoder,decodeSelection,decodeSelectionPage,decodeDirectory,selectionBody,emptyReaderFilters } from '../../src/features/audience-api';
import type { Audience,Reader } from '../../src/features/audience-api';
import { loadAudience,loadSelection } from '../../src/features/audience-load';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport } from '../../src/api/transport';
import { fixture,json,tenant,otherTenant } from './api-fixtures';

const did='77777777-7777-4777-8777-777777777777';const sid='88888888-8888-4888-8888-888888888888';
function reader(id=tenant,active=true):Reader {return {principal_id:id,membership_id:id,display_name:'Nama utuh',principal_active:active,membership_active:true,eligible:active};}
const current:Audience={document_id:did,document_version:3,access_revision:4,audience_mode:'specific_people',explicit_readers_count:1,readers:[reader()],next_after:null};
const receipt={selection_id:sid,selected_count:1,snapshot_digest:'a'.repeat(64),expires_at:1900000000,creator_grant_included:false,base_document_id:did,expected_document_version:3,collection_id:null,retain_existing:true};
const wire=(value:Audience=current) => ({...value,other_authorities:['knowledge_manager'],dynamic_employee_role:value.audience_mode==='all_current_employees'});
it('distinguishes named empty from all employees and validates authority/count/pagination metadata',() => {
  expect(audienceDecoder(did)(wire({...current,readers:[],explicit_readers_count:0})).audience_mode).toBe('specific_people');
  for (const change of [{document_id:tenant},{audience_mode:'all_current_employees'},{other_authorities:[]},{next_after:otherTenant},{readers:[reader(),reader()]}]) expect(() => audienceDecoder(did)({...wire(),...change})).toThrow();
});
it('keeps inactive existing grants via server retention and removes them only on explicit replacement',() => {
  const inactive=reader(tenant,false);const base={...current,readers:[inactive]};
  const retained=selectionBody(null,base,[inactive,reader(otherTenant)],new Set(),emptyReaderFilters,false);
  expect(retained).toMatchObject({retain_existing:true,include_ids:[otherTenant],exclude_ids:[]});
  expect(selectionBody(null,base,[],new Set(),emptyReaderFilters,false)).toMatchObject({retain_existing:false,include_ids:[],exclude_ids:[]});
});
it('binds filters only to bulk creation and preserves manually excluded choices without truncating payload',() => {
  const filters={...emptyReaderFilters,department:'Finance',job_title:'Manager'};
  expect(selectionBody(null,current,[reader()],new Set([otherTenant]),filters,true)).toMatchObject({all_matching:true,filters:{department:'Finance',job_title:'Manager'},exclude_ids:[otherTenant]});
  expect(() => selectionBody(null,null,Array.from({length:1001},() => reader()),new Set(),filters,true)).toThrow();
});
it('validates immutable review metadata, row eligibility, creator grant and count guards',() => {
  expect(decodeSelection({...receipt,base_document_id:null,expected_document_version:null,creator_grant_included:true}).creator_grant_included).toBe(true);
  for (const change of [{selected_count:1001},{expected_document_version:null},{snapshot_digest:'bad'}]) expect(() => decodeSelection({...receipt,...change})).toThrow();
  expect(() => decodeSelectionPage({...receipt,items:[reader()],next_after:null,consumed:true})).toThrow();
  expect(() => decodeDirectory({items:[reader(tenant,false)],next_after:null})).toThrow();
});
it('discards all assembled ACL pages when a later page changes CAS, counts, or ordering',async () => {
  const ordered=[tenant,otherTenant].sort();
  for (const drift of [true,false]) {
    const api=new ApiCoordinator(new ApiTransport(async path => path.endsWith('/me')?json(fixture()):json(wire({...current,document_version:path.includes('after=')&&drift?4:3,explicit_readers_count:2,readers:[reader(path.includes('after=')?ordered[1]:ordered[0])],next_after:path.includes('after=')?null:ordered[0]}),200,{'X-EAI-Context-Binding':'a'.repeat(64)})),{request:async (_name,_options,work) => work()});const stamp=await api.bootstrap();
    const result=loadAudience(api,stamp,did,new AbortController().signal);
    if (drift) await expect(result).rejects.toMatchObject({kind:'conflict'});else expect((await result).readers).toHaveLength(2);api.dispose();
  }
});
it('requires every snapshot page to match the frozen receipt and exact total',async () => {
  const api=new ApiCoordinator(new ApiTransport(async path => path.endsWith('/me')?json(fixture()):json({...receipt,snapshot_digest:'b'.repeat(64),items:[reader()],next_after:null,consumed:false},200,{'X-EAI-Context-Binding':'a'.repeat(64)})),{request:async (_name,_options,work) => work()});const stamp=await api.bootstrap();
  await expect(loadSelection(api,stamp,receipt,new AbortController().signal)).rejects.toMatchObject({kind:'conflict'});api.dispose();
});
