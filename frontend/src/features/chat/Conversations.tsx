import {useCallback,useEffect,useId,useMemo,useRef,useState} from 'react';
import {Link,useNavigate} from 'react-router';
import {object} from '../../api/transport';
import {ApiFailure} from '../../api/failure';
import {Button} from '../../components/Button';
import {SelectField} from '../../components/Fields';
import {StatusMessage} from '../../components/StatusMessage';
import {usePreferences} from '../../app/preference-context';
import {useUnsavedInput} from '../../app/unsaved-context';
import {ResourceState,WriteState} from '../workspace';
import {useCopy,useResource,useWorkspace,useWrite} from '../workspace-hooks';
import {decodeConversations,decodeCreated,decodeRenamed,validName} from './conversation-api';
import type {Conversation,Language} from './conversation-api';
import {SourceWorkspace} from '../SourceWorkspace';
import type {SourceReference} from '../source-api';
import {ChatMessages,RunMonitor} from './ChatTurn';
import {useLiveHistory} from './use-live-history';
import {ChatDrafts} from './drafts';
import {useChatDraft} from './use-chat-draft';
import {ConversationEntry} from './AssistedComposer';
import {WorkflowResult} from './WorkflowResult';
import {RunDiagnostics} from './RunDiagnostics';
import {retryDeadline} from './diagnostics-api';
import {chatHistoryDecoder,lastHistoryPage} from './turn-api';
import type {Run,ChatHistory} from './turn-api';

function usePendingBody(resource:string) {
  const {api,stamp}=useWorkspace();
  return ()=>{
    const intent=api.intents.pending(resource,stamp.generation);
    return intent?object(object(JSON.parse(intent.material) as unknown).body):undefined;
  };
}
function languageName(language:Language){return language==='id'?'Indonesia':'English';}

function CreateConversation() {
  const c=useCopy();const {locale}=usePreferences();const navigate=useNavigate();
  const [choice,setChoice]=useState<Language>();const pendingBody=usePendingBody('POST /api/v1/conversations');
  const [created,setCreated]=useState<string>();
  const decode=useCallback((value:unknown)=>decodeCreated(value,pendingBody()?.language),[pendingBody]);
  const write=useWrite('POST','/api/v1/conversations',decode);
  const pending=pendingBody();const language=(pending?.language ?? choice ?? locale) as Language;
  useUnsavedInput(!created&&!!choice);
  // Navigate only after the settled write and local choice have committed, so
  // the unsaved-input guard does not ask to discard an already-created intent.
  useEffect(()=>{if(created)void navigate('/chat/'+created);},[created,navigate]);
  async function create(reconcile=false){const row=await write.execute(reconcile?undefined:{language});if(row){setChoice(undefined);setCreated(row.id);}}
  return <section className="conversation-create" aria-label={c('Percakapan baru','New conversation')}>
    <form className="conversation-create-controls" onSubmit={e=>{e.preventDefault();void create();}}>
      <SelectField label={c('Bahasa percakapan baru','New conversation language')} value={language} disabled={write.busy||!!write.intent}
        onChange={e=>setChoice(e.target.value as Language)} options={[{value:'id',label:'Indonesia'},{value:'en',label:'English'}]}/>
      <Button type="submit" variant="primary" disabled={write.busy||!!write.intent}>{c('Buat percakapan','Create conversation')}</Button>
    </form><p className="field-hint">{c('Bahasa dipilih sebelum percakapan dibuat dan tetap sama meskipun bahasa tampilan berubah.','Choose the language before creating a conversation. Changing the interface language will not change it.')}</p>
    <WriteState error={write.error} unknown={write.unknown} busy={write.busy} reconcile={()=>{void create(true);}}/>
  </section>;
}

export function ConversationList() {
  const c=useCopy();const [cursors,setCursors]=useState<string[]>([]);const cursor=cursors.at(-1);
  const result=useResource('/api/v1/conversations?limit=20'+(cursor?'&after='+cursor:''),decodeConversations);
  return <><div className="page-heading"><h1>{c('Percakapan','Conversations')}</h1><p>{c('Percakapan Anda dalam perusahaan yang sedang aktif.','Your conversations in the current company.')}</p></div>
    <CreateConversation/>
    <ResourceState {...result} retry={result.refresh}/>
    {result.data&&<>{result.data.items.length===0?<StatusMessage kind="empty" title={c('Belum ada percakapan','No conversations yet')}><p>{c('Pilih bahasa, lalu buat percakapan pertama Anda.','Choose a language and create your first conversation.')}</p></StatusMessage>:
      <ul className="conversation-list">{result.data.items.map(row=><li key={row.id}><h2><Link className="conversation-label" to={'/chat/'+row.id}>{row.label??c('Percakapan baru','New conversation')}</Link></h2><p className="field-hint">{c('Bahasa percakapan: ','Conversation language: ')}{languageName(row.language)}</p></li>)}</ul>}
      <nav className="workspace-actions" aria-label={c('Halaman percakapan','Conversation pages')}><Button disabled={!cursors.length} onClick={()=>setCursors(cursors.slice(0,-1))}>{c('Sebelumnya','Previous')}</Button><Button disabled={!result.data.next_after} onClick={()=>{if(result.data?.next_after)setCursors([...cursors,result.data.next_after]);}}>{c('Berikutnya','Next')}</Button><Button onClick={result.refresh}>{c('Muat ulang percakapan','Reload conversations')}</Button></nav></>}
  </>;
}

function RenameConversation({conversation,refresh}:{conversation:Conversation;refresh:()=>void}) {
  const c=useCopy();const inputId=useId();const errorRef=useRef<HTMLDivElement>(null);
  const path='/api/v1/conversations/'+conversation.id+'/name';const pendingBody=usePendingBody('PATCH '+path);
  const decode=useCallback((value:unknown)=>{const body=pendingBody();if(!body)throw new ApiFailure('malformed');return decodeRenamed(value,conversation.id,body);},[pendingBody,conversation.id]);
  const write=useWrite('PATCH',path,decode);const pending=pendingBody();
  const [baseVersion,setBaseVersion]=useChatDraft('rename:version',String(pending?.expected_version??conversation.version));const [editing,setEditing]=useChatDraft('rename:editing',!!pending);const [name,setName]=useChatDraft('rename:name',typeof pending?.name==='string'?pending.name:conversation.label??'');const [invalid,setInvalid]=useState(false);
  useEffect(()=>{if(invalid)errorRef.current?.focus();},[invalid]);
  useUnsavedInput(editing&&name!==(conversation.label??''));
  async function save(reconcile=false){
    if(!reconcile&&!validName(name)){setInvalid(true);return;}
    const receipt=await write.execute(reconcile?undefined:{name,expected_version:Number(baseVersion)});
    if(receipt){setEditing(false);refresh();}
  }
  return <section className="conversation-rename">{!editing&&!write.intent?<Button onClick={()=>{setName(conversation.label??'');setBaseVersion(String(conversation.version));setEditing(true);}}>{c('Ganti nama percakapan','Rename conversation')}</Button>:<form onSubmit={e=>{e.preventDefault();void save();}}>
    {invalid&&<div ref={errorRef} role="alert" tabIndex={-1}><p id={inputId+'-error'} className="field-error">{c('Nama wajib diisi, maksimal 4096 karakter dan 8192 byte, tanpa karakter kontrol.','Enter a name of up to 4096 characters and 8192 bytes without control characters.')}</p></div>}
    <div className="field"><label htmlFor={inputId}>{c('Nama percakapan','Conversation name')}</label><textarea id={inputId} rows={3} value={typeof pending?.name==='string'?pending.name:name} disabled={write.busy||!!write.intent} aria-invalid={invalid} aria-describedby={invalid?inputId+'-error':undefined} onChange={e=>{setName(e.target.value);setInvalid(false);}}/></div>
    <div className="workspace-actions"><Button type="submit" disabled={write.busy||!!write.intent}>{c('Simpan nama','Save name')}</Button><Button disabled={write.busy||!!write.intent} onClick={()=>{setEditing(false);setName(conversation.label??'');setInvalid(false);}}>{c('Batal','Cancel')}</Button></div>
  </form>}
    <WriteState error={write.error} unknown={write.unknown} busy={write.busy} reconcile={()=>{void save(true);}}/>
    {write.error?.kind==='conflict'&&!write.unknown&&<Button onClick={()=>{setEditing(false);setName(conversation.label??'');setBaseVersion(String(conversation.version));refresh();}}>{c('Muat ulang percakapan','Reload conversation')}</Button>}
  </section>;
}

export function ConversationPage({id}:{id:string}) {return <ChatDrafts><ConversationDetail id={id}/></ChatDrafts>;}
function ConversationDetail({id}:{id:string}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const [cursors,setCursors]=useState<number[]>([]);const after=cursors.at(-1)??0;
  const decode=useMemo(()=>chatHistoryDecoder(id,after),[id,after]);
  const path='/api/v1/conversations/'+id+'/messages?after='+after+'&limit=20';
  const result=useLiveHistory(path,decode);const [tracked,setTracked]=useState<string>();const [outcome,setOutcome]=useState<Run>();const [resolving,setResolving]=useState(false);const [missing,setMissing]=useState(false);
  const [validated,setValidated]=useState<{identity:string;data:ChatHistory}>();
  const history=validated?.identity===result.identity?validated.data:result.data;
  const assistedRuns=useRef(new Set<string>());const [assistedRevision,setAssistedRevision]=useState(0);const entryStarted=useRef(false);const [entryError,setEntryError]=useState<ApiFailure>();const [entryUntil,setEntryUntil]=useState(0);
  useEffect(()=>{if(entryUntil){const timer=setTimeout(()=>setEntryUntil(0),Math.max(0,entryUntil-Date.now()));return()=>clearTimeout(timer);}},[entryUntil]);
  const [returnChecking,setReturnChecking]=useState(false);const [returnFailed,setReturnFailed]=useState(false);
  const [anchor,setAnchor]=useState<{message:string;reference:SourceReference}>();
  const detail=useRef<HTMLDivElement>(null);const settled=useRef(new Set<string>());const pendingRead=useRef<AbortController|undefined>(undefined);
  useEffect(()=>()=>pendingRead.current?.abort(),[]);
  const refreshHistory=result.refresh;
  const discoverLatest=useCallback(async(first:ChatHistory)=>{
    pendingRead.current?.abort();const controller=new AbortController();pendingRead.current=controller;
    await Promise.resolve();if(controller.signal.aborted)return;setResolving(true);setEntryError(undefined);
    try{
      const latest=await lastHistoryPage(first,after=>api.resourceRead({path:'/api/v1/conversations/'+id+'/messages?after='+after+'&limit=20',decode:chatHistoryDecoder(id,after)},stamp,controller.signal));
      if(!controller.signal.aborted){setCursors([latest.after]);refreshHistory();}
    }catch(error){if(!controller.signal.aborted){const failure=error instanceof ApiFailure?error:new ApiFailure('unavailable');setEntryError(failure);setEntryUntil(retryDeadline(failure));}}
    finally{if(!controller.signal.aborted)setResolving(false);}
  },[api,stamp,id,refreshHistory]);
  useEffect(()=>{
    const first=result.data;if(entryStarted.current||!first)return;let active=true;
    queueMicrotask(()=>{if(!active||entryStarted.current)return;entryStarted.current=true;if(first.next_after!==null&&!first.conversation.active_run_id)void discoverLatest(first);});
    return()=>{active=false;};
  },[result.data,discoverLatest]);
  async function terminal(run:Run,force=false,clear?:()=>void){
    const key=run.id+':'+run.version;if(!force&&settled.current.has(key))return;settled.current.add(key);clear?.();setTracked(run.id);setOutcome(run);
    if(run.state!=='completed'||assistedRuns.current.has(run.id)){result.refresh();return;}
    pendingRead.current?.abort();const controller=new AbortController();pendingRead.current=controller;setResolving(true);setMissing(false);
    try{
      let cursor=0;let found=false;
      for(let page=0;page<10;page++){
        const next=await api.resourceRead({path:'/api/v1/conversations/'+id+'/messages?after='+cursor+'&limit=20',decode:chatHistoryDecoder(id,cursor)},stamp,controller.signal);
        if(next.messages.some(m=>m.run_id===run.id&&m.author_kind==='model')){if(!controller.signal.aborted){setCursors([cursor]);result.refresh();}found=true;break;}
        if(next.next_after===null)break;cursor=next.next_after;
      }
      if(!controller.signal.aborted&&!found)setMissing(true);
    }catch{if(!controller.signal.aborted)setMissing(true);}
    finally{if(!controller.signal.aborted)setResolving(false);}
  }
  const recognizeWorkflow=useCallback((run:string)=>{if(assistedRuns.current.has(run))return;assistedRuns.current.add(run);setAssistedRevision(n=>n+1);if(outcome?.id===run&&outcome.state==='completed'){pendingRead.current?.abort();setMissing(false);setResolving(false);}},[outcome]);
  const verifyReturn=async()=>{
    if(!anchor)return false;
    setReturnChecking(true);setReturnFailed(false);
    try{
      const current=await api.resourceRead({path,decode},stamp);
      const allowed=current.messages.some(m=>m.id===anchor.message&&!m.redacted&&m.citations.some(ref=>ref.version_id===anchor.reference.version_id&&ref.label===anchor.reference.label&&ref.text_digest===anchor.reference.text_digest&&ref.quote===anchor.reference.quote&&ref.page===anchor.reference.page&&ref.chunk_no===anchor.reference.chunk_no&&ref.version_no===anchor.reference.version_no));
      if(allowed){setValidated({identity:result.identity,data:current});if(!result.data)result.refresh();}else result.refresh();return allowed;
    }catch{setReturnFailed(true);result.refresh();return false;}
    finally{setReturnChecking(false);}
  };
  const workflowRun=history?.conversation.active_run_id??tracked??history?.messages.filter(m=>m.run_id).at(-1)?.run_id??undefined;
  return <SourceWorkspace refreshKey={result.cycle} suspended={!history||result.masked} verifyReturn={verifyReturn} resolveReturnTrigger={()=>anchor?detail.current?.querySelector<HTMLElement>('[data-message-id="'+anchor.message+'"] [data-source-label="'+anchor.reference.label+'"]')??null:null} onUnavailable={result.refresh} onReturnStart={()=>{setReturnChecking(true);setReturnFailed(false);}} onReturnError={()=>{setReturnFailed(true);result.refresh();}} onReturnEnd={()=>setReturnChecking(false)}>{(open,clear)=><div ref={detail} className="conversation-detail"><Link to="/chat">{c('Kembali ke percakapan','Back to conversations')}</Link><ResourceState {...result} retry={result.refresh} retryBlocked={result.retryBlocked}/>
    {entryError&&<ResourceState loading={false} error={entryError} retryBlocked={!!entryUntil} retry={()=>{if(result.data&&!entryUntil)void discoverLatest(result.data);}}/>}
    {returnChecking&&<StatusMessage kind="loading" title={c('Memeriksa akses percakapan','Checking conversation access')}/>}
    {returnFailed&&<StatusMessage kind="error" title={c('Akses percakapan belum terverifikasi','Conversation access has not been verified')}><p>{c('Isi dan penilaian disembunyikan sampai akses dapat diperiksa.','Content and feedback remain hidden until access can be checked.')}</p><Button onClick={()=>{void verifyReturn();}}>{c('Periksa akses percakapan','Check conversation access')}</Button></StatusMessage>}
    {history&&<><div className="page-heading"><h1 className="conversation-label">{history.conversation.label??c('Percakapan baru','New conversation')}</h1><p>{c('Bahasa percakapan: ','Conversation language: ')}{languageName(history.conversation.language)}</p></div>
      <RenameConversation key={id+':'+history.conversation.version} conversation={history.conversation} refresh={result.refresh}/>
      {(history.conversation.active_run_id??tracked)&&<RunMonitor id={(history.conversation.active_run_id??tracked)!} conversation={id} onTerminal={run=>{void terminal(run,false,clear);}}/>}
      {resolving&&<StatusMessage kind="loading" title={c('Memuat hasil terbaru','Loading latest result')}/>}
      {missing&&<StatusMessage kind="error" title={c('Hasil terbaru belum dapat dimuat','Latest result could not be loaded')}><p>{c('Proses telah selesai, tetapi hasilnya belum terverifikasi. Tidak ada pertanyaan baru yang dikirim.','Processing has finished, but its result has not been verified. No new question was sent.')}</p><Button onClick={()=>{if(outcome)void terminal(outcome,true);}}>{c('Periksa hasil terbaru','Check latest result')}</Button></StatusMessage>}
      <section aria-label={c('Pesan tersimpan','Saved messages')}>{!missing&&!resolving&&!returnFailed&&!entryError&&<div hidden={returnChecking}>{history.messages.length===0?<StatusMessage kind="empty" title={c('Belum ada pesan','No messages yet')}/>:<ChatMessages messages={history.messages} refresh={()=>{clear();result.refresh();}} open={(ref,trigger)=>{const message=history!.messages.find(m=>m.citations.some(item=>item===ref));if(message)setAnchor({message:message.id,reference:ref});open(ref,trigger);}}/>}</div>}
        <nav className="workspace-actions" aria-label={c('Halaman pesan','Message pages')}><Button disabled={!cursors.length} onClick={()=>{pendingRead.current?.abort();setResolving(false);setEntryError(undefined);setEntryUntil(0);clear();setCursors(cursors.slice(0,-1));}}>{c('Pesan sebelumnya','Previous messages')}</Button><Button disabled={!history.next_after} onClick={()=>{if(history?.next_after){pendingRead.current?.abort();setResolving(false);setEntryError(undefined);setEntryUntil(0);clear();setCursors([...cursors,history.next_after]);}}}>{c('Pesan berikutnya','Next messages')}</Button></nav>
      </section>
    </>}
    {(history?.conversation.active_run_id??tracked??history?.messages.filter(m=>m.run_id).at(-1)?.run_id)&&<RunDiagnostics key={history?.conversation.active_run_id??tracked??history?.messages.filter(m=>m.run_id).at(-1)?.run_id} id={(history?.conversation.active_run_id??tracked??history?.messages.filter(m=>m.run_id).at(-1)?.run_id)!} blocked={!history||result.masked} recovered={()=>{setOutcome(undefined);setTracked(undefined);result.refresh();}}/>}
    {history&&!result.masked&&!returnChecking&&!returnFailed&&workflowRun&&<WorkflowResult key={'workflow:'+workflowRun!} id={workflowRun!} conversation={id} known={assistedRuns.current.has(workflowRun!)} cycle={result.cycle+assistedRevision} onKnown={recognizeWorkflow} onResume={()=>{setTracked(workflowRun);setOutcome(undefined);result.refresh();}}/>}
    <ConversationEntry id={id} blocked={!history||result.masked||!!history.conversation.active_run_id||resolving||missing||!!entryError||returnChecking||returnFailed||!!tracked&&!outcome} accepted={(run,sequence,assisted)=>{if(assisted)recognizeWorkflow(run);clear();setTracked(run);setOutcome(undefined);setCursors([Math.max(0,sequence-1)]);result.refresh();}}/>

  </div>}</SourceWorkspace>;
}
