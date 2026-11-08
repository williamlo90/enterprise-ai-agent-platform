import {ApiFailure} from '../../api/failure';
import {object} from '../../api/transport';
import {runDecoder} from './turn-api';
import type {RunState} from './turn-api';
const phases=['begin','retrieve','generate','provider','publish','hold','recover'];
const outcomes=['started','completed','failed','deadline','cancelled','budget','unknown','closed_without_replay'];
const number=(v:unknown):v is number=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
const integer=(v:unknown):v is number=>number(v)&&Number.isSafeInteger(v);
export interface Diagnostics {id:string;state:RunState;version:number;recovered_at:string|null;recovery_available:boolean;recovery:'closed_without_replay'|null;steps:number;max_steps:number;deadline:number|null;events:{phase:string;outcome:string;sequence:number;duration_ms:number}[];usage:{known_input_tokens:number;known_output_tokens:number;reserved_input_tokens:number;reserved_output_tokens:number;effective_input_tokens:number;effective_output_tokens:number;unknown_attempts:number;attempts:number;overrun:boolean;rate_card:string;known_estimate_usd:number;unknown_reserved_estimate_usd:number}}
export function diagnosticsDecoder(id:string) {
  return(value:unknown):Diagnostics=>{
    const r=object(value);runDecoder(id,'')( {...r,conversation_id:''});
    if(r.recovered_at!==null&&(typeof r.recovered_at!=='string'||!Number.isFinite(Date.parse(r.recovered_at)))||typeof r.recovery_available!=='boolean'||r.recovery!==null&&r.recovery!=='closed_without_replay'||!integer(r.steps)||!integer(r.max_steps)||r.max_steps!==8||r.steps>8||r.deadline!==null&&!number(r.deadline)||!Array.isArray(r.events)||r.events.length>20)throw new ApiFailure('malformed');
    let sequence=0;const events=r.events.map(value=>{const e=object(value);if(!phases.includes(String(e.phase))||!outcomes.includes(String(e.outcome))||!integer(e.sequence)||e.sequence<=sequence||!integer(e.duration_ms)||e.duration_ms>3600000)throw new ApiFailure('malformed');sequence=e.sequence;return {phase:e.phase as string,outcome:e.outcome as string,sequence:e.sequence,duration_ms:e.duration_ms};});
    const u=object(r.usage);const counts=['known_input_tokens','known_output_tokens','reserved_input_tokens','reserved_output_tokens','effective_input_tokens','effective_output_tokens','unknown_attempts','attempts'] as const;
    if(counts.some(k=>!integer(u[k]))||Number(u.unknown_attempts)>Number(u.attempts)||typeof u.overrun!=='boolean'||u.rate_card!=='prototype-nova-apac-cohere-sg-v1'||!number(u.known_estimate_usd)||!number(u.unknown_reserved_estimate_usd))throw new ApiFailure('malformed');
    // Project only the established metadata allowlist, never render response JSON.
    const usage={...Object.fromEntries(counts.map(k=>[k,u[k]])),overrun:u.overrun,rate_card:u.rate_card,known_estimate_usd:u.known_estimate_usd,unknown_reserved_estimate_usd:u.unknown_reserved_estimate_usd} as Diagnostics['usage'];
    return {id,state:r.state as RunState,version:r.version as number,recovered_at:r.recovered_at as string|null,recovery_available:r.recovery_available,recovery:r.recovery as Diagnostics['recovery'],steps:r.steps,max_steps:r.max_steps,deadline:r.deadline as number|null,events,usage};
  };
}
export function recoverDecoder(id:string,expected:number) {return(value:unknown)=>{const row=diagnosticsDecoder(id)(value);if(row.version!==expected+1||row.state!=='failed'||row.recovery!=='closed_without_replay'||!row.recovered_at||row.recovery_available)throw new ApiFailure('malformed');return row;};}

export const retryDeadline=(failure:ApiFailure)=>Date.now()+(failure.retryAfterMs??(failure.status===429?15000:0));
