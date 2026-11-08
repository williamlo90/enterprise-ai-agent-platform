import {expect,it} from 'vitest';
import {decodeConversations,decodeCreated,decodeRenamed,historyDecoder,validName} from '../../src/features/chat/conversation-api';
const id='11111111-1111-4111-8111-111111111111';
const messageId='22222222-2222-4222-8222-222222222222';
const row={id,language:'id',version:1,label:null,label_source:'empty',active_run_id:null,created_at:'2026-11-06T00:00:00Z',updated_at:'2026-11-06T00:00:00Z'};
it('preserves full first question labels and rejects duplicate/corrupt pages',()=>{
  const text='<b>pertanyaan</b> '+ 'x'.repeat(4000);
  expect(decodeConversations({items:[{...row,label:text,label_source:'first_question'}],next_after:null}).items[0].label).toBe(text);
  expect(()=>decodeConversations({items:[row,row],next_after:null})).toThrow();
  expect(()=>decodeConversations({items:[row],next_after:messageId})).toThrow();
  expect(()=>decodeConversations({items:[{...row,label:'bad'}],next_after:null})).toThrow();
});
it('matches Unicode codepoint/UTF8 bounds without trimming or rejecting allowed whitespace',()=>{
  expect(validName('  Nama\n\t lengkap  ')).toBe(true);
  expect(validName('😀'.repeat(2048))).toBe(true);
  expect(validName('😀'.repeat(2049))).toBe(false);
  expect(validName('x'.repeat(4097))).toBe(false);
  expect(validName(' \n\t')).toBe(false);
  expect(validName('A\u0001B')).toBe(false);
  expect(validName('\ud800')).toBe(false);
});
it('binds create to original language but accepts current replay metadata',()=>{
  expect(decodeCreated({...row,version:4,label:'Later label',label_source:'manual'},'id').version).toBe(4);
  expect(()=>decodeCreated(row,'en')).toThrow();
});
it('binds rename receipts to ID, original text and CAS before settlement',()=>{
  const body={name:'  Nama lengkap\n',expected_version:1};
  const receipt={id,label:body.name,label_source:'manual',version:2};
  expect(decodeRenamed(receipt,id,body).version).toBe(2);
  for(const bad of [{...receipt,id:messageId},{...receipt,label:'Nama lengkap'},{...receipt,version:3}])expect(()=>decodeRenamed(bad,id,body)).toThrow();
});
it('binds deep-link history and pagination, projecting only safe text fields',()=>{
  const message={id:messageId,sequence:1,author_kind:'human',text:'<script>not executed</script>',redacted:false};
  expect(historyDecoder(id,0)({conversation:row,messages:[message],next_after:1}).messages[0].text).toBe(message.text);
  expect(()=>historyDecoder(messageId,0)({conversation:row,messages:[],next_after:null})).toThrow();
  expect(()=>historyDecoder(id,1)({conversation:row,messages:[message],next_after:null})).toThrow();
});
it('never accepts contradictory redacted content or leaked sources',()=>{
  const message={id:messageId,sequence:1,author_kind:'model',text:null,redacted:true,sources:[],citations:[],feedback:null};
  expect(historyDecoder(id,0)({conversation:row,messages:[message],next_after:null}).messages[0].redacted).toBe(true);
  expect(()=>historyDecoder(id,0)({conversation:row,messages:[{...message,text:'private'}],next_after:null})).toThrow();
  expect(()=>historyDecoder(id,0)({conversation:row,messages:[{...message,sources:[{}]}],next_after:null})).toThrow();
});
it('keeps safe stored system messages distinct from model messages',()=>{
  const message={id:messageId,sequence:1,author_kind:'system',text:'Pengajuan diterima.',redacted:false};
  expect(historyDecoder(id,0)({conversation:row,messages:[message],next_after:null}).messages[0].author_kind).toBe('system');
});
