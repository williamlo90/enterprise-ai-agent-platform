import {ApiFailure} from '../../api/failure';
import {isUuid,object} from '../../api/transport';

export type Language='id'|'en';
export interface Conversation {id:string;language:Language;version:number;label:string|null;label_source:'empty'|'first_question'|'manual';created_at:string;updated_at:string;active_run_id:string|null}
const integer=(value:unknown,minimum=1):value is number=>Number.isSafeInteger(value)&&(value as number)>=minimum;
export function validName(value:string):boolean {
  const points=[...value];
  return value.replaceAll('\u0085','').trim().length>0&&points.length<=4096&&new TextEncoder().encode(value).length<=8192&&!points.some(c=>{
    const code=c.codePointAt(0)!;
    return code<32&&!['\r','\n','\t'].includes(c)||code===127||code>=0xd800&&code<=0xdfff;
  });
}
export function decodeConversation(value:unknown):Conversation {
  const row=object(value);
  if(!isUuid(row.id)||!['id','en'].includes(String(row.language))||!integer(row.version)||
    row.active_run_id!==null&&!isUuid(row.active_run_id)||!['empty','first_question','manual'].includes(String(row.label_source))||
    (row.label_source==='empty'?row.label!==null:typeof row.label!=='string'||!validName(row.label))||
    ![row.created_at,row.updated_at].every(v=>typeof v==='string'&&v.length<=64&&Number.isFinite(Date.parse(v))))throw new ApiFailure('malformed');
  return {id:row.id,language:row.language as Language,version:row.version,active_run_id:row.active_run_id as string|null,label:row.label as string|null,label_source:row.label_source as Conversation['label_source'],created_at:row.created_at as string,updated_at:row.updated_at as string};
}
export function decodeConversations(value:unknown):{items:Conversation[];next_after:string|null} {
  const row=object(value);
  if(!Array.isArray(row.items)||row.items.length>20||row.next_after!==null&&!isUuid(row.next_after))throw new ApiFailure('malformed');
  const items=row.items.map(decodeConversation);
  if(new Set(items.map(v=>v.id)).size!==items.length||row.next_after!==null&&(!items.length||items.at(-1)?.id!==row.next_after))throw new ApiFailure('malformed');
  return {items,next_after:row.next_after as string|null};
}
export function decodeCreated(value:unknown,language:unknown):Conversation {
  const row=decodeConversation(value);
  // Create replay returns current metadata, which can already have messages or
  // a later manual label. Only the original language is immutable here.
  if(row.language!==language)throw new ApiFailure('malformed');
  return row;
}
export function decodeRenamed(value:unknown,id:string,body:Record<string,unknown>):{id:string;version:number} {
  const row=object(value);
  if(row.id!==id||row.label!==body.name||row.label_source!=='manual'||!integer(body.expected_version)||row.version!==body.expected_version+1)throw new ApiFailure('malformed');
  return {id,version:row.version as number};
}
export interface SavedMessage {id:string;sequence:number;author_kind:'human'|'model'|'system';text:string|null;redacted:boolean}
export interface SavedConversation {conversation:Conversation;messages:SavedMessage[];next_after:number|null}
export function historyDecoder(id:string,after:number) {
  return (value:unknown):SavedConversation=>{
    const row=object(value);const conversation=decodeConversation(row.conversation);
    if(conversation.id!==id||!Array.isArray(row.messages)||row.messages.length>20||row.next_after!==null&&!integer(row.next_after))throw new ApiFailure('malformed');
    let sequence=after;
    const messages=row.messages.map(value=>{
      const message=object(value);
      if(!isUuid(message.id)||!integer(message.sequence)||message.sequence<=sequence||!['human','model','system'].includes(String(message.author_kind))||typeof message.redacted!=='boolean'||
        (message.redacted?message.text!==null||!Array.isArray(message.sources)||message.sources.length!==0||!Array.isArray(message.citations)||message.citations.length!==0||message.feedback!==null:typeof message.text!=='string'||message.text.length>65536))throw new ApiFailure('malformed');
      sequence=message.sequence;
      return {id:message.id,sequence,author_kind:message.author_kind as SavedMessage['author_kind'],text:message.text as string|null,redacted:message.redacted};
    });
    if(new Set(messages.map(m=>m.id)).size!==messages.length||row.next_after!==null&&(!messages.length||row.next_after!==sequence))throw new ApiFailure('malformed');
    return {conversation,messages,next_after:row.next_after as number|null};
  };
}
