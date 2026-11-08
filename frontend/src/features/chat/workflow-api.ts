import {ApiFailure} from '../../api/failure';
import {object} from '../../api/transport';
import {identifier,nullableId,positive} from '../knowledge-api';
import {array,claim,draft,action,page} from '../reimbursement/contracts';
import type {Receipt,Action,Claim,Draft,Page} from '../reimbursement/contracts';
import type {ApiCoordinator,ContextStamp} from '../../app/api-coordinator';
export interface Selection {kind:'submission'|'information_update';trip_id:string;claim_id:string|null;receipts:Receipt[]}
export function selection(value:unknown):Selection{const r=object(value);if(!['submission','information_update'].includes(r.kind as string)||r.kind==='submission'&&r.claim_id!==null||r.kind==='information_update'&&r.claim_id===null)throw new ApiFailure('input');const receipts=array(r.receipts,20).map(v=>{const f=object(v);return {id:identifier(f.id),version:positive(f.version)};});if(new Set(receipts.map(f=>f.id)).size!==receipts.length)throw new ApiFailure('input');return {kind:r.kind as Selection['kind'],trip_id:identifier(r.trip_id),claim_id:nullableId(r.claim_id),receipts};}
export interface Handle {run_id:string;draft_id:string|null;clarification_reason:'missing_amount'|'missing_receipt'|'ambiguous_request'|null}
export function handleDecoder(id:string){identifier(id);return(value:unknown):Handle=>{const r=object(value);if(r.run_id!==id||r.clarification_reason!==null&&!['missing_amount','missing_receipt','ambiguous_request'].includes(String(r.clarification_reason)))throw new ApiFailure('malformed');const draft_id=nullableId(r.draft_id);if(draft_id&&r.clarification_reason!==null)throw new ApiFailure('malformed');return {run_id:id,draft_id,clarification_reason:r.clarification_reason as Handle['clarification_reason']};};}
export function assistedDraftDecoder(id:string,conversation:string){return(v:unknown)=>{const d=draft(v);if(d.id!==id||object(v).conversation_id!==conversation)throw new ApiFailure('malformed');return d;};}
// Current ACT/source status, never the graph ACK or model prose. Bounded serial
// pages preserve discovery without pretending absence beyond the inspected range.
export async function delivery(api:ApiCoordinator,stamp:ContextStamp,d:Draft,signal:AbortSignal):Promise<{action?:Action;claim?:Claim;bounded:boolean}>{
 let after:string|null=null;
 for(let n=0;n<10;n++){
  const p:Page=await api.resourceRead({path:'/api/v1/actions?limit=20'+(after?'&after='+after:''),decode:page},stamp,signal);
  const candidates=p.items.map(action).filter(a=>a.draft_id===d.id&&a.draft_version===d.version);
  if(candidates.length>1)throw new ApiFailure('malformed');
  if(candidates[0]){const a=await api.resourceRead({path:'/api/v1/actions/'+candidates[0].operation_id,decode:action},stamp,signal);if(a.operation_id!==candidates[0].operation_id||a.draft_id!==d.id||a.draft_version!==d.version)throw new ApiFailure('malformed');if(a.state==='committed'&&a.result_ref){const c=await api.resourceRead({path:'/api/v1/claims/'+a.result_ref,decode:claim},stamp,signal);if(c.id!==a.result_ref||c.trip_id!==d.trip_id||c.version<(a.result_version??0))throw new ApiFailure('malformed');return {action:a,claim:c,bounded:false};}return {action:a,bounded:false};}
  if(p.next===null)return {bounded:false};if(p.next===after)throw new ApiFailure('malformed');after=p.next;
 }
 return {bounded:true};
}
