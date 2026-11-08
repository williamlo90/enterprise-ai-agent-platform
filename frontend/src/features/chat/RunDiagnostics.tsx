import {useCallback,useEffect,useMemo,useState} from 'react';
import {Button} from '../../components/Button';
import {StatusMessage} from '../../components/StatusMessage';
import {ApiFailure} from '../../api/failure';
import {object} from '../../api/transport';
import {ResourceState,WriteState} from '../workspace';
import {useCopy,useWorkspace,useWrite} from '../workspace-hooks';
import {usePrivateRead} from '../use-private-read';
import {useChatDraft} from './use-chat-draft';
import {diagnosticsDecoder,recoverDecoder,retryDeadline} from './diagnostics-api';

export function RunDiagnostics({id,blocked,recovered}:{id:string;blocked:boolean;recovered:()=>void}) {
  const c=useCopy();const [open,setOpen]=useChatDraft('diagnostics:'+id+':open',false);
  return <section className="run-diagnostics"><Button disabled={blocked} onClick={()=>setOpen(!open)}>{open?c('Sembunyikan detail proses','Hide processing details'):c('Lihat detail proses','View processing details')}</Button>
    {open&&!blocked&&<Details id={id} recovered={recovered}/>}</section>;
}
function Details({id,recovered}:{id:string;recovered:()=>void}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const path='/api/v1/runs/'+id;
  const decode=useMemo(()=>diagnosticsDecoder(id),[id]);const [request,setRequest]=useState(0);const current=usePrivateRead(path+'/diagnostics',decode,request);const result={...current,refresh:()=>{if(!current.cooling)setRequest(v=>v+1);}};
  const [confirm,setConfirm]=useState<number>();const [checking,setChecking]=useState(false);const [readError,setReadError]=useState<ApiFailure>();const [readUntil,setReadUntil]=useState(0);
  useEffect(()=>{if(readUntil){const timer=setTimeout(()=>setReadUntil(0),Math.max(0,readUntil-Date.now()));return()=>clearTimeout(timer);}},[readUntil]);const [retryVersion,setRetryVersion]=useState<number>();
  const receipt=useCallback((value:unknown)=>{const intent=api.intents.pending('POST '+path+'/recover',stamp.generation);if(!intent)throw new ApiFailure('malformed');const body=object(object(JSON.parse(intent.material) as unknown).body);if(typeof body.expected_version!=='number')throw new ApiFailure('malformed');return recoverDecoder(id,body.expected_version)(value);},[api,stamp,path,id]);
  const write=useWrite('POST',path+'/recover',receipt);const row=result.data;
  async function close(){const version=confirm;setConfirm(undefined);if(version===undefined)return;const data=await write.execute(write.unknown?undefined:{expected_version:version});if(data){recovered();result.refresh();}else result.refresh();}
  async function inspectUnknown(){
    if(readUntil)return;setChecking(true);setReadError(undefined);setRetryVersion(undefined);
    try{
      const current=await api.resourceRead({path:path+'/diagnostics',decode},stamp);
      const intent=api.intents.pending('POST '+path+'/recover',stamp.generation);
      if(intent?.state==='unknown'){
        const expected=object(object(JSON.parse(intent.material) as unknown).body).expected_version;
        // CAS is not an idempotency ledger: observe closure, never claim which
        // caller won. A changed version invalidates the old confirmation.
        if(current.state==='failed'&&current.recovered_at||current.version!==expected){api.intents.transition(intent,'settled',stamp.generation);api.intents.discard(intent.key);recovered();}
        else if(current.recovery_available)setRetryVersion(current.version);
      }
      result.refresh();
    }catch(error){const failure=error instanceof ApiFailure?error:new ApiFailure('unavailable');setReadError(failure);setReadUntil(retryDeadline(failure));}
    finally{setChecking(false);}
  }
  const phaseNames:Record<string,string>={begin:c('Mulai','Start'),retrieve:c('Mencari sumber','Retrieving sources'),generate:c('Menyusun jawaban','Preparing answer'),provider:c('Layanan model','Model service'),publish:c('Menyimpan hasil','Saving result'),hold:c('Ditahan','Held'),recover:c('Penutupan','Closure')};
  const outcomeNames:Record<string,string>={started:c('Dimulai','Started'),completed:c('Selesai','Completed'),failed:c('Tidak berhasil','Failed'),deadline:c('Batas waktu tercapai','Deadline reached'),cancelled:c('Eksekusi terhenti','Execution stopped'),budget:c('Batas pemakaian tercapai','Usage limit reached'),unknown:c('Belum pasti','Unknown'),closed_without_replay:c('Ditutup tanpa menjalankan ulang','Closed without replay')};
  return <div className="run-diagnostics-body"><ResourceState {...result} retry={result.refresh} retryBlocked={result.cooling}/>
    {row&&<><h2>{c('Detail proses','Processing details')}</h2>
      <dl><dt>{c('Langkah terpakai','Steps used')}</dt><dd>{row.steps} / {row.max_steps}</dd><dt>{c('Batas waktu pemrosesan','Processing deadline')}</dt><dd>{row.deadline===null?c('Belum dimulai; antrean tidak dihitung sebagai waktu pemrosesan.','Not started; queue time is separate from processing time.'):new Date(row.deadline*1000).toLocaleString('en-GB',{timeZone:'Asia/Singapore'})+' (Asia/Singapore)'}</dd></dl>
      <p>{c('Token terukur: input ','Measured tokens: input ')}{row.usage.known_input_tokens}{c(', output ',', output ')}{row.usage.known_output_tokens}.</p>
      <p>{c('Token dicadangkan: input ','Reserved tokens: input ')}{row.usage.reserved_input_tokens}{c(', output ',', output ')}{row.usage.reserved_output_tokens}.</p>
      <p>{c('Token yang diperhitungkan pada batas pemakaian: input ','Tokens counted against usage limits: input ')}{row.usage.effective_input_tokens}{c(', output ',', output ')}{row.usage.effective_output_tokens}.</p>
      {row.usage.unknown_attempts>0&&<p role="status">{c('Pemakaian belum pasti untuk ','Usage remains unknown for ')}{row.usage.unknown_attempts}{c(' panggilan. Cadangan tetap diperhitungkan; ini bukan pemakaian atau biaya nol.',' calls. Reservations remain counted; usage and cost are not zero.')}</p>}
      <p>{c('Estimasi pemakaian terukur US$','Known usage estimate US$')}{row.usage.known_estimate_usd.toFixed(6)}{c('; cadangan belum pasti US$','; unknown reservation US$')}{row.usage.unknown_reserved_estimate_usd.toFixed(6)}. {c('Tarif prototipe, bukan tagihan atau harga terbaru.','Prototype rates, not an invoice or current pricing.')}</p>
      {row.usage.overrun&&<p>{c('Pemakaian terukur melebihi cadangan.','Measured usage exceeds its reservation.')}</p>}
      {row.recovered_at&&<p role="status">{c('Proses sudah ditutup tanpa menjalankan ulang model.','Processing was closed without replaying the model.')}</p>}
      <h3>{c('Tahap yang tercatat','Recorded phases')}</h3><ol>{row.events.map(event=><li key={event.sequence}>{phaseNames[event.phase]} · {outcomeNames[event.outcome]} · {event.duration_ms} ms</li>)}</ol>
      <Button disabled={result.cooling} onClick={()=>{setConfirm(undefined);setRetryVersion(undefined);result.refresh();}}>{c('Periksa detail terbaru','Check current details')}</Button>
      {!write.intent&&row.recovery_available&&<Button onClick={()=>setConfirm(row.version)}>{c('Tutup proses tertahan','Close stalled processing')}</Button>}
      {write.unknown&&<StatusMessage kind="error" title={c('Hasil penutupan belum diketahui','Closure result unknown')}><p>{c('Periksa status server terlebih dahulu. Tidak ada model yang dijalankan ulang.','Check the server state first. No model is replayed.')}</p><Button disabled={checking||write.busy||!!readUntil} onClick={()=>{void inspectUnknown();}}>{c('Periksa status penutupan','Check closure status')}</Button>{retryVersion===row.version&&row.recovery_available&&<Button onClick={()=>setConfirm(row.version)}>{c('Tutup dengan versi yang sama','Close with the same version')}</Button>}</StatusMessage>}
      {confirm===row.version&&row.recovery_available&&<div role="group" aria-label={c('Konfirmasi penutupan proses','Confirm closure')}><p>{c('Proses ini akan ditutup tanpa membuat jawaban atau menjalankan model ulang. Anda dapat mengirim pertanyaan baru setelah status diperiksa kembali.','This closes processing without producing an answer or replaying the model. You can ask a new question after its state is checked again.')}</p><Button disabled={write.busy||checking} onClick={()=>{void close();}}>{c('Ya, tutup tanpa menjalankan ulang','Yes, close without replay')}</Button><Button disabled={write.busy} onClick={()=>setConfirm(undefined)}>{c('Batal','Cancel')}</Button></div>}
    </>}
    {!write.unknown&&<WriteState error={write.error} unknown={false} busy={write.busy} reconcile={()=>{}}/>}
    {readError&&<ResourceState loading={false} error={readError} retryBlocked={!!readUntil} retry={()=>{void inspectUnknown();}}/>}
  </div>;
}
