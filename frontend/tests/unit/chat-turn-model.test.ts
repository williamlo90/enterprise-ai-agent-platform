import {expect,it} from 'vitest';
import {describeWrite,writeKeyField} from '../../src/api/write-policy';
import {chatHistoryDecoder,decodeAdmission,decodeFeedback,runDecoder,terminalRun,validReason} from '../../src/features/chat/turn-api';
const c='77777777-7777-4777-8777-777777777777',r='88888888-8888-4888-8888-888888888888',m='99999999-9999-4999-8999-999999999999';
const conversation={id:c,language:'id',version:3,label:'Pertanyaan',label_source:'first_question',active_run_id:null,created_at:'2026-11-06T00:00:00Z',updated_at:'2026-11-06T00:00:00Z'};
const message={id:m,sequence:2,author_kind:'model',run_id:r,output_kind:'answer',text:'Jawaban',redacted:false,sources:[],citations:[],feedback:null};
it('selects the message idempotency field and forbids caller-overridden keys',()=>{
  const path='/api/v1/conversations/'+c+'/messages';expect(writeKeyField('POST',path)).toBe('client_message_id');expect(describeWrite({method:'POST',path,body:{text:'Test'}}).status).toBe(202);
  expect(()=>describeWrite({method:'POST',path,body:{text:'Test',client_message_id:r}})).toThrow();expect(writeKeyField('POST','/api/v1/messages/'+m+'/feedback')).toBe('key');
});
it('binds admission and run metadata, accepting current run state on replay',()=>{
  const value={conversation_id:c,client_message_id:m,message_id:r,sequence:21,run_id:r,run_state:'completed',conversation_version:4};
  expect(decodeAdmission(value,c,m).sequence).toBe(21);expect(()=>decodeAdmission(value,c,r)).toThrow();expect(()=>runDecoder(r,c)({id:r,conversation_id:m,state:'queued',version:1})).toThrow();expect(terminalRun({id:r,conversation_id:c,state:'held',version:2})).toBe(true);
});
it('matches citation IDs to explicit source identity rather than source array position',()=>{
  const ref={system:'platform',kind:'document_version',resource_id:c,version:'2',page:3,chunk:4};
  const wire={...message,sources:[{...ref,resource_id:r},ref],citations:[{id:'S2',version_id:c,page:3,chunk_no:4,text_digest:'a'.repeat(64),quote:'😀 sumber'}]};
  const parsed=chatHistoryDecoder(c,0)({conversation,messages:[wire],next_after:null});expect(parsed.messages[0].citations[0]).toMatchObject({label:'S2',version_id:c,version_no:2,chunk_no:4,quote:'😀 sumber'});
  expect(()=>chatHistoryDecoder(c,0)({conversation,messages:[{...wire,sources:[]}],next_after:null})).toThrow();
});
it('keeps redaction authoritative for feedback, citations and text',()=>{
  const redacted={...message,redacted:true,text:null};expect(chatHistoryDecoder(c,0)({conversation,messages:[redacted],next_after:null}).messages[0].redacted).toBe(true);
  expect(()=>chatHistoryDecoder(c,0)({conversation,messages:[{...redacted,feedback:{rating:'up',reason:null,version:1}}],next_after:null})).toThrow();
});
it('validates feedback against accepted intent while allowing a newer current vote',()=>{
  expect(decodeFeedback({message_id:m,accepted_rating:'down',result_version:1,current_rating:'up',feedback_version:2},m,'down').message_id).toBe(m);
  expect(()=>decodeFeedback({message_id:m,accepted_rating:'up',result_version:1,current_rating:'up',feedback_version:2},m,'down')).toThrow();expect(validReason('')).toBe(true);expect(validReason('😀'.repeat(1000))).toBe(true);expect(validReason('x'.repeat(1001))).toBe(false);expect(validReason('bad\u0001')).toBe(false);
});
