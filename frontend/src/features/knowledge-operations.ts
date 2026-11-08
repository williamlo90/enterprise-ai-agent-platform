import { ApiFailure } from '../api/failure';
import { decodeWrite } from '../api/write-policy';
import type { JsonWrite } from '../api/write-policy';
import type { ApiCoordinator,ContextStamp } from '../app/api-coordinator';
import type { Intent } from '../api/intents';
import { identifier } from './knowledge-api';
import { admissionDecoder,lifecycleDecoder } from './knowledge-lifecycle-api';
import type { KnowledgeOperationContext,KnowledgeReceipt } from './knowledge-lifecycle-api';

interface Entry {readonly key:string;readonly generation:number;readonly context:Readonly<KnowledgeOperationContext>}
const owners=new WeakMap<ApiCoordinator,Map<string,Entry>>();
const terminals=new WeakMap<ApiCoordinator,Set<string>>();
function records(api:ApiCoordinator) {
  let entries=owners.get(api);
  if (!entries) {entries=new Map();owners.set(api,entries);terminals.set(api,new Set());const current=entries;api.subscribeInvalidation(() => {current.clear();terminals.get(api)?.clear();});}
  return entries;
}
export function knowledgeWithdrawn(api:ApiCoordinator,stamp:ContextStamp,documentId:string):boolean {
  try {api.identityView(stamp);return terminals.get(api)?.has(documentId) ?? false;} catch {return false;}
}
export function knowledgePending(api:ApiCoordinator,stamp:ContextStamp):{entry:Entry;intent:Intent}[] {
  try {api.identityView(stamp);} catch {return [];}
  const result:{entry:Entry;intent:Intent}[]=[];
  for (const entry of records(api).values()) {
    const intent=api.intents.current(entry.key);
    if (!intent) {records(api).delete(entry.key);continue;}
    if (entry.generation===stamp.generation && ['prepared','dispatching','unknown'].includes(intent.state)) result.push({entry,intent});
  }
  return result;
}
export async function knowledgeExecute(api:ApiCoordinator,stamp:ContextStamp,request?:JsonWrite,context?:KnowledgeOperationContext,reconcileKey?:string):Promise<KnowledgeReceipt> {
  const entries=records(api);let entry:Entry;let intent:Intent;
  if (reconcileKey) {
    const pending=knowledgePending(api,stamp).find((row) => row.entry.key===reconcileKey);
    if (!pending || pending.intent.state!=='unknown') throw new ApiFailure('conflict');entry=pending.entry;intent=pending.intent;
  } else {
    if (!request || !context || knowledgePending(api,stamp).some((row) => row.entry.context.documentId===context.documentId)) throw new ApiFailure('conflict');
    if (context.documentId && knowledgeWithdrawn(api,stamp,context.documentId)) throw new ApiFailure('conflict');
    if (context.documentId) identifier(context.documentId);if (context.collectionId) identifier(context.collectionId);
    const path=context.kind==='create'?'/api/v1/knowledge/documents':context.kind==='version'?'/api/v1/knowledge/documents/'+context.documentId+'/versions':context.kind==='publish'?'/api/v1/knowledge/versions/'+identifier(context.versionId)+'/publish':'/api/v1/knowledge/documents/'+context.documentId+'/withdraw';
    if (request.method!=='POST' || request.path!==path || (context.kind==='create')!==(context.documentId===null) ||
      (context.kind==='create' || context.kind==='version') && (request.body.file_id!==context.fileId || !context.fileId)) throw new ApiFailure('input');
    intent=api.prepareWrite(request,stamp);entry=Object.freeze({key:intent.key,generation:stamp.generation,context:Object.freeze({...context})});entries.set(intent.key,entry);
  }
  const body=decodeWrite(intent.material).body;const bound=entry.context;
  const decode=(value:unknown):KnowledgeReceipt => {
    const result:KnowledgeReceipt=bound.kind==='create' || bound.kind==='version'?{kind:'admission',value:admissionDecoder(bound,body)(value)}:{kind:'lifecycle',value:lifecycleDecoder(bound,body)(value)};
    // Install terminal masking before IntentStore emits settlement/removal.
    if (result.kind==='lifecycle' && result.value.withdrawn) terminals.get(api)?.add(result.value.document_id);
    return result;
  };
  try {const receipt=await api.dispatchWrite(intent.key,stamp,decode,{reconcile:!!reconcileKey});entries.delete(intent.key);return receipt;}
  catch (error) {
    const current=api.intents.current(intent.key);
    if (current?.state==='prepared') api.discardPrepared(intent.key);
    if (!api.intents.current(intent.key)) entries.delete(intent.key);
    throw error;
  }
}
