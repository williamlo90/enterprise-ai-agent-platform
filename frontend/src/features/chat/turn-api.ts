import {ApiFailure} from '../../api/failure';
import {isUuid,object} from '../../api/transport';
import {historyDecoder} from './conversation-api';
import type {SavedMessage,Conversation} from './conversation-api';
import type {SourceReference} from '../source-api';
export type RunState='queued'|'running'|'waiting'|'held'|'completed'|'failed';
export interface Run {id:string;conversation_id:string;state:RunState;version:number}
const states=['queued','running','waiting','held','completed','failed'];
const positive=(v:unknown):v is number=>Number.isSafeInteger(v)&&(v as number)>0;
export function runDecoder(id:string,conversation:string){return(value:unknown):Run=>{const r=object(value);if(r.id!==id||r.conversation_id!==conversation||!states.includes(String(r.state))||!positive(r.version))throw new ApiFailure('malformed');return {id,conversation_id:conversation,state:r.state as RunState,version:r.version};};}
export const terminalRun=(run:Run)=>run.state!=='queued'&&run.state!=='running';
export function decodeAdmission(value:unknown,conversation:string,clientId:string) {
  const r=object(value);if(r.conversation_id!==conversation||r.client_message_id!==clientId||!isUuid(r.message_id)||!isUuid(r.run_id)||!positive(r.sequence)||!positive(r.conversation_version)||!states.includes(String(r.run_state)))throw new ApiFailure('malformed');
  return {run_id:r.run_id,sequence:r.sequence,message_id:r.message_id};
}
export interface Citation extends SourceReference {label:string;quote:string;chunk_no:number;page:number;text_digest:string}
export interface ChatMessage extends SavedMessage {run_id:string|null;output_kind:string|null;citations:Citation[];feedback:{rating:'up'|'down';reason:string|null;version:number}|null}
export interface ChatHistory {conversation:Conversation;messages:ChatMessage[];next_after:number|null}
const kinds=['answer','clarification','insufficient_evidence','source_conflict','service_unavailable'];
export function chatHistoryDecoder(id:string,after:number) {
  const base=historyDecoder(id,after);
  return(value:unknown):ChatHistory=>{
    const parsed=base(value),raw=object(value);const messages=raw.messages as unknown[];
    return {...parsed,messages:parsed.messages.map((m,index)=>{
      const r=object(messages[index]);
      if(r.run_id!==null&&!isUuid(r.run_id)||r.output_kind!==null&&!kinds.includes(String(r.output_kind)))throw new ApiFailure('malformed');
      if(!Array.isArray(r.citations)||r.citations.length>2||!Array.isArray(r.sources)||r.sources.length>16)throw new ApiFailure('malformed');
      const citations=r.citations.map(value=>{
        const c=object(value);if(typeof c.id!=='string'||!/^S[1-5]$/.test(c.id)||!isUuid(c.version_id)||!positive(c.page)||!Number.isSafeInteger(c.chunk_no)||(c.chunk_no as number)<0||typeof c.text_digest!=='string'||!/^[a-f0-9]{64}$/.test(c.text_digest)||typeof c.quote!=='string'||!c.quote.length||new TextEncoder().encode(c.quote).length>16384)throw new ApiFailure('malformed');
        const ref=r.sources as unknown[];const source=ref.map(object).find(s=>s.system==='platform'&&s.kind==='document_version'&&s.resource_id===c.version_id&&s.page===c.page&&s.chunk===c.chunk_no);
        if(!source||!positive(Number(source.version)))throw new ApiFailure('malformed');
        return {version_id:c.version_id,version_no:Number(source.version),label:c.id,quote:c.quote,page:c.page,chunk_no:c.chunk_no as number,text_digest:c.text_digest};
      });
      if(new Set(citations.map(c=>c.label)).size!==citations.length)throw new ApiFailure('malformed');
      let feedback:ChatMessage['feedback']=null;
      if(r.feedback!==null){const f=object(r.feedback);if(!['up','down'].includes(String(f.rating))||!positive(f.version)||f.reason!==null&&(typeof f.reason!=='string'||!validReason(f.reason)))throw new ApiFailure('malformed');feedback={rating:f.rating as 'up'|'down',version:f.version,reason:f.reason as string|null};}
      if(m.redacted&&(citations.length||feedback))throw new ApiFailure('malformed');
      return {...m,run_id:r.run_id as string|null,output_kind:r.output_kind as string|null,citations,feedback};
    })};
  };
}
export function validReason(value:string):boolean {
  return [...value].length<=1000&&new TextEncoder().encode(value).length<=4096&&![...value].some(c=>{const n=c.codePointAt(0)!;return n<32&&!['\r','\n','\t'].includes(c)||n===127||n>=0xd800&&n<=0xdfff;});
}
export function decodeFeedback(value:unknown,id:string,rating:unknown) {
  const r=object(value);if(r.message_id!==id||r.accepted_rating!==rating||!['up','down'].includes(String(r.current_rating))||!positive(r.result_version)||!positive(r.feedback_version)||r.result_version>r.feedback_version)throw new ApiFailure('malformed');return {message_id:id};
}
export async function lastHistoryPage(first:ChatHistory,read:(after:number)=>Promise<ChatHistory>) {
  let data=first,after=0;
  for(let page=1;page<10&&data.next_after!==null;page++){after=data.next_after;data=await read(after);}
  if(data.next_after!==null)throw new ApiFailure('unavailable');
  return {after,data};
}
