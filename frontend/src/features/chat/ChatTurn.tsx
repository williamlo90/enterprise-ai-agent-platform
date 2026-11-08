import {useCallback,useEffect,useId,useMemo,useRef,useState} from 'react';
import {Button} from '../../components/Button';
import {object} from '../../api/transport';
import {ApiFailure} from '../../api/failure';
import {useUnsavedInput} from '../../app/unsaved-context';
import {useCopy,useWorkspace,useWrite} from '../workspace-hooks';
import {WriteState} from '../workspace';
import {useStatusPoll} from '../use-status-poll';
import type {OpenSource} from '../SourceWorkspace';
import {validName} from './conversation-api';
import {decodeAdmission,decodeFeedback,runDecoder,terminalRun,validReason} from './turn-api';
import {useChatDraft} from './use-chat-draft';
import type {ChatMessage,Run} from './turn-api';

export function MessageComposer({id,blocked,accepted}:{id:string;blocked:boolean;accepted:(run:string,sequence:number)=>void}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const path='/api/v1/conversations/'+id+'/messages';const field=useId();const [text,setText]=useState('');const [invalid,setInvalid]=useState(false);
  const decode=useCallback((value:unknown)=>{const intent=api.intents.pending('POST '+path,stamp.generation);if(!intent)throw new ApiFailure('malformed');return decodeAdmission(value,id,intent.key);},[api,stamp,id,path]);
  const write=useWrite('POST',path,decode);const pending=write.intent?object(object(JSON.parse(write.intent.material) as unknown).body):undefined;
  const current=typeof pending?.text==='string'?pending.text:text;useUnsavedInput(!!text);
  async function send(reconcile=false){if(!reconcile&&!validName(text)){setInvalid(true);return;}const receipt=await write.execute(reconcile?undefined:{text});if(receipt){setText('');setInvalid(false);accepted(receipt.run_id,receipt.sequence);}}
  return <section className="chat-composer"><form onSubmit={e=>{e.preventDefault();void send();}}><div className="field"><label htmlFor={field}>{c('Pertanyaan Anda','Your question')}</label><textarea id={field} rows={3} value={current} disabled={blocked||write.busy||!!write.intent} aria-invalid={invalid} aria-describedby={invalid?field+'-error':undefined} onChange={e=>{setText(e.target.value);setInvalid(false);}}/>
    {invalid&&<p className="field-error" role="alert" id={field+'-error'}>{c('Isi pertanyaan, maksimal 4096 karakter dan 8192 byte, tanpa karakter kontrol.','Enter a question up to 4096 characters and 8192 bytes without control characters.')}</p>}</div><Button type="submit" disabled={blocked||write.busy||!!write.intent}>{c('Kirim pertanyaan','Send question')}</Button></form>
    {blocked&&!write.unknown&&<p role="status">{c('Tunggu proses pertanyaan sebelumnya selesai.','Wait for the previous question to finish processing.')}</p>}
    <WriteState error={write.error} unknown={write.unknown} busy={write.busy} reconcile={()=>{void send(true);}}/>
  </section>;
}

export function RunMonitor({id,conversation,onTerminal}:{id:string;conversation:string;onTerminal:(run:Run)=>void}) {
  const c=useCopy();const decode=useMemo(()=>runDecoder(id,conversation),[id,conversation]);const result=useStatusPoll('/api/v1/runs/'+id,decode,terminalRun);const notified=useRef('');
  useEffect(()=>{if(result.data&&terminalRun(result.data)){const key=result.data.id+':'+result.data.version;if(notified.current!==key){notified.current=key;onTerminal(result.data);}}},[result.data,onTerminal]);
  const labels={queued:c('Pertanyaan diterima, menunggu pemrosesan','Question accepted, awaiting processing'),running:c('Sedang memproses pertanyaan','Processing your question'),completed:c('Pemrosesan selesai','Processing finished'),failed:c('Pemrosesan tidak berhasil','Processing did not succeed'),waiting:c('Proses menunggu tindak lanjut','Processing is waiting for further action'),held:c('Proses ditahan','Processing is held')};
  return <section className="chat-run" aria-live="polite"><h2>{result.data?labels[result.data.state]:c('Memeriksa status pertanyaan','Checking question status')}</h2>
    {result.error&&<p>{c('Status belum dapat diperiksa. Ini tidak berarti pertanyaan gagal atau dibatalkan.','Status could not be checked. This does not mean the question failed or was cancelled.')}</p>}
    {['bounded','paused'].includes(result.phase)&&<p>{c('Pemeriksaan otomatis berhenti. Periksa kembali untuk melihat status terbaru.','Automatic checking has stopped. Check again for the latest status.')}</p>}
    <Button disabled={result.retryBlocked||result.phase==='loading'} onClick={result.refresh}>{c('Periksa status pertanyaan','Check question status')}</Button>
  </section>;
}

function Feedback({message,refresh}:{message:ChatMessage;refresh:()=>void}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const path='/api/v1/messages/'+message.id+'/feedback';const field=useId();const [down,setDown]=useChatDraft('feedback:'+message.id+':editing',false);const [reason,setReason]=useChatDraft('feedback:'+message.id+':reason','');const [invalid,setInvalid]=useState(false);
  const decode=useCallback((value:unknown)=>{const intent=api.intents.pending('POST '+path,stamp.generation);if(!intent)throw new ApiFailure('malformed');const body=object(object(JSON.parse(intent.material) as unknown).body);return decodeFeedback(value,message.id,body.rating);},[api,stamp,path,message.id]);
  const write=useWrite('POST',path,decode);useUnsavedInput(!!reason);async function save(rating:'up'|'down',reconcile=false){if(!reconcile&&!validReason(reason)){setInvalid(true);return;}const receipt=await write.execute(reconcile?undefined:{rating,reason:rating==='down'&&reason!==''?reason:null});if(receipt){setDown(false);setReason('');refresh();}}
  return <section className="chat-feedback" aria-label={c('Penilaian jawaban','Answer feedback')}>
    {message.feedback&&<p role="status">{c('Penilaian tersimpan: ','Saved feedback: ')}{message.feedback.rating==='up'?c('Membantu','Helpful'):c('Tidak membantu','Not helpful')}</p>}
    <div className="workspace-actions"><Button disabled={write.busy||!!write.intent} onClick={()=>{void save('up');}}>{c('Membantu','Helpful')}</Button><Button disabled={write.busy||!!write.intent} onClick={()=>setDown(true)}>{c('Tidak membantu','Not helpful')}</Button></div>
    {down&&<form onSubmit={e=>{e.preventDefault();void save('down');}}><div className="field"><label htmlFor={field}>{c('Alasan, opsional','Reason, optional')}</label><textarea id={field} rows={2} value={reason} disabled={write.busy||!!write.intent} onChange={e=>{setReason(e.target.value);setInvalid(false);}}/></div>{invalid&&<p className="field-error" role="alert">{c('Alasan maksimal 1000 karakter dan 4096 byte tanpa karakter kontrol.','Reason may contain up to 1000 characters and 4096 bytes without control characters.')}</p>}<Button type="submit" disabled={write.busy||!!write.intent}>{c('Simpan penilaian','Save feedback')}</Button></form>}
    <WriteState error={write.error} unknown={write.unknown} busy={write.busy} reconcile={()=>{void save('down',true);}}/>
  </section>;
}

export function ChatMessages({messages,open,refresh}:{messages:ChatMessage[];open:OpenSource;refresh:()=>void}) {
  const c=useCopy();const titles={answer:c('Jawaban','Answer'),clarification:c('Perlu penjelasan tambahan','More information needed'),insufficient_evidence:c('Informasi belum cukup','Insufficient information'),source_conflict:c('Sumber memberikan informasi berbeda','Sources contain conflicting information'),service_unavailable:c('Layanan belum tersedia','Service unavailable')};
  return <ol className="conversation-messages">{messages.map(m=><li key={m.id} data-message-id={m.id}><h2>{m.author_kind==='human'?c('Anda','You'):m.author_kind==='system'?c('Sistem','System'):titles[m.output_kind as keyof typeof titles]??c('Asisten','Assistant')}</h2>
    {m.redacted?<p>{c('Isi pesan tidak tersedia karena akses sumber berubah.','Message content is unavailable because source access changed.')}</p>:<><p className="conversation-text">{m.text}</p>
      {m.citations.map(ref=><Button key={ref.label} data-source-label={ref.label} variant="quiet" onClick={e=>open(ref,e.currentTarget)}>{c('Buka sumber ','Open source ')+ref.label}</Button>)}
      {['insufficient_evidence','source_conflict'].includes(m.output_kind??'')&&<p>{c('Hubungi pengelola pengetahuan perusahaan untuk memastikan informasi yang diperlukan.','Contact your company knowledge manager to confirm the information you need.')}</p>}
      {m.author_kind==='model'&&<Feedback key={m.id+':'+(m.feedback?.version??0)} message={m} refresh={refresh}/>}</>}
  </li>)}</ol>;
}
