import { useCallback,useEffect,useMemo,useRef,useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { Button } from '../components/Button';
import { StatusMessage } from '../components/StatusMessage';
import { SourceHighlight } from '../components/SourceHighlight';
import { sourceDecoder,sourceLocation } from './source-api';
import type { SourceReference } from './source-api';
import { usePrivateRead } from './use-private-read';
import { useCopy,useWorkspace } from './workspace-hooks';
import type { ApiFailure } from '../api/failure';

export function PrivateReadError({error,cooling,retry}:{error:ApiFailure;cooling:boolean;retry:()=>void}) {
  const c=useCopy();const denied=error.kind==='not_found' || error.kind==='forbidden';
  return <StatusMessage kind="error" title={denied?c('Sumber tidak tersedia','Source unavailable'):c('Data belum dapat dimuat','Data could not be loaded')}>
    <p>{denied?c('Sumber tidak tersedia atau akses Anda telah berubah.','The source is unavailable or your access has changed.'):c('Periksa kembali untuk mengambil data terbaru.','Check again to retrieve current data.')}</p>
    {cooling && <p>{c('Tunggu sebelum memeriksa kembali.','Wait before checking again.')}</p>}
    <Button disabled={cooling} onClick={retry}>{c('Periksa kembali','Check again')}</Button>
  </StatusMessage>;
}

function SourceReader({reference,onUnavailable,revision=0,focusOnLoad=true,onFocused}:{reference:SourceReference;onUnavailable?:()=>void;revision?:number;focusOnLoad?:boolean;onFocused?:()=>void}) {
  const c=useCopy();const [request,setRequest]=useState(0);const heading=useRef<HTMLHeadingElement>(null);const target=useRef<HTMLElement>(null);const focused=useRef(false);
  const decode=useMemo(() => {const decodeSource=sourceDecoder(reference.version_id);return (value:unknown) => {const source=decodeSource(value);return {source,location:sourceLocation(source,reference)};};},[reference]);
  const result=usePrivateRead('/api/v1/knowledge/sources/'+reference.version_id+'/content',decode,request+revision);
  const unavailable=result.error?.kind==='not_found' || result.error?.kind==='forbidden';
  useEffect(() => {if (unavailable) onUnavailable?.();},[unavailable,onUnavailable]);
  useEffect(() => {const element=result.data?.location?target.current:heading.current;if (element&&!focused.current&&focusOnLoad) {focused.current=true;onFocused?.();element.focus({preventScroll:true});element.scrollIntoView?.({block:'nearest'});}},[result.data,focusOnLoad,onFocused]);
  if (result.loading) return <StatusMessage kind="loading" title={c('Memuat sumber','Loading source')} />;
  if (result.error) return <PrivateReadError error={result.error} cooling={result.cooling} retry={() => setRequest((value) => value+1)} />;
  if (!result.data) return null;
  const {source,location}=result.data;
  return <><header className="source-heading"><h2 tabIndex={-1} ref={heading}>{source.title}</h2>
    <p>{c('Versi ','Version ')+source.version_no} · {source.publication_state==='superseded'?c('Versi lama — telah digantikan','Previous version — superseded'):c('Diterbitkan','Published')}</p>
    {reference.label && <p>{c('Referensi ','Reference ')+reference.label}</p>}
    <p className="field-hint">{c('Teks hasil ekstraksi','Extracted text')}</p>
    {(source.valid_from || source.valid_until) && <p className="field-hint">{c('Periode sumber (Asia/Singapore): mulai inklusif ','Source period (Asia/Singapore): inclusive from ')}{source.valid_from??'—'}{c(', sampai eksklusif ',', exclusive until ')}{source.valid_until??'—'}</p>}
    <Button disabled={result.cooling} onClick={() => setRequest((value) => value+1)}>{c('Periksa akses terbaru','Check current access')}</Button>
  </header>
    {source.chunks.map((chunk) => <section key={chunk.chunk_no} ref={location?.chunk.chunk_no===chunk.chunk_no?target:undefined} tabIndex={-1} className="source-chunk" aria-label={c('Halaman ','Page ')+chunk.page+', '+c('chunk ','chunk ')+chunk.chunk_no}>
      <h3>{c('Halaman ','Page ')+chunk.page} · {c('Chunk ','Chunk ')+chunk.chunk_no}</h3><p className="field-hint">{c('Posisi teks ','Text position ')+chunk.start+'–'+chunk.end}</p>
      <SourceHighlight text={chunk.text} ranges={location?.chunk.chunk_no===chunk.chunk_no?location.ranges:[]} />
    </section>)}
  </>;
}

export type OpenSource=(reference:SourceReference,trigger:HTMLElement)=>void;
// Chat supplies verifyReturn to revalidate its conversation/message anchor before
// restoring position. The workspace itself always verifies the session context.
export function SourceWorkspace({children,refreshKey=0,suspended=false,onUnavailable,verifyReturn,resolveReturnTrigger,onReturn,onReturnStart,onReturnError,onReturnEnd}:{children:(open:OpenSource,clear:()=>void)=>ReactNode;refreshKey?:number;suspended?:boolean;onUnavailable?:()=>void;verifyReturn?:()=>Promise<boolean>;resolveReturnTrigger?:()=>HTMLElement|null;onReturn?:()=>void;onReturnStart?:()=>void;onReturnError?:()=>void;onReturnEnd?:()=>void}) {
  const c=useCopy();const {api,stamp}=useWorkspace();const [selected,setSelected]=useState<{reference:SourceReference;serial:number}>();const [focusedSerial,setFocusedSerial]=useState(0);const serial=useRef(0);const alive=useRef(true);const main=useRef<HTMLDivElement>(null);
  const unavailableSerial=useRef(0);
  const unavailable=useCallback(()=>{if(selected&&unavailableSerial.current!==selected.serial){unavailableSerial.current=selected.serial;onUnavailable?.();}},[selected,onUnavailable]);
  const saved=useRef<{trigger:HTMLElement;x:number;y:number;top:number}|undefined>(undefined);
  useEffect(() => {alive.current=true;return () => {alive.current=false;saved.current=undefined;};},[]);
  const open=useCallback((reference:SourceReference,trigger:HTMLElement) => {saved.current={trigger,x:window.scrollX,y:window.scrollY,top:main.current?.scrollTop??0};setSelected({reference,serial:++serial.current});},[]);
  const clear=useCallback(() => {saved.current=undefined;serial.current++;setSelected(undefined);},[]);
  async function close() {
    onReturnStart?.();
    const previous=saved.current;saved.current=undefined;const attempt=++serial.current;setSelected(undefined);
    try {
      const current=await api.bootstrap();
      if (current.generation!==stamp.generation || current.binding!==stamp.binding || current.fingerprint!==stamp.fingerprint) {onReturnError?.();return;}
      if (verifyReturn && !await verifyReturn()) return;
      await new Promise<void>(resolve=>requestAnimationFrame(()=>resolve()));
      const trigger=resolveReturnTrigger?.()??previous?.trigger;
      if (!alive.current || serial.current!==attempt || !previous || !trigger?.isConnected) return;
      if (main.current) main.current.scrollTop=previous.top;trigger.focus({preventScroll:true});window.scrollTo(previous.x,previous.y);onReturn?.();
    } catch {onReturnError?.(); /* Never restore stale focus or content after a failed check. */}
    finally {onReturnEnd?.();}
  }
  // The render prop attaches these callbacks to event handlers; it never invokes
  // them while rendering. Their refs hold event-only focus/scroll snapshots.
  // eslint-disable-next-line react-hooks/refs
  return <div className={'source-workspace'+(selected?' source-workspace--open':'')}><div ref={main} className="source-workspace-main">{children(open,clear)}</div>
    <aside className="source-workspace-panel" aria-label={c('Sumber','Source')} hidden={!selected}>
      {selected && <><Button onClick={() => {void close();}}>{c('Kembali ke posisi sebelumnya','Back to previous position')}</Button>{suspended?<StatusMessage kind="loading" title={c('Memeriksa akses percakapan','Checking conversation access')}/>:<SourceReader key={selected.serial} reference={selected.reference} onUnavailable={unavailable} revision={refreshKey} focusOnLoad={focusedSerial!==selected.serial} onFocused={()=>setFocusedSerial(selected.serial)} />}</>}
    </aside></div>;
}

export function SourcePage({versionId}:{versionId:string}) {
  const c=useCopy();const reference=useMemo(() => ({version_id:versionId}),[versionId]);
  return <div className="source-page"><Link to="/search">{c('Kembali ke pencarian','Back to search')}</Link><SourceReader reference={reference} /></div>;
}
