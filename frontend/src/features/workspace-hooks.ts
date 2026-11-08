import { useCallback,useContext,useEffect,useRef,useState,useSyncExternalStore } from 'react';
import { WorkspaceContext } from './workspace-context';
import type { Workspace } from './workspace-context';
import { ApiFailure } from '../api/failure';
import type { Decoder } from '../api/transport';
import type { JsonWrite } from '../api/write-policy';
import { useUnsavedInput } from '../app/unsaved-context';
import { usePreferences } from '../app/preference-context';

export function useWorkspace():Workspace {const value=useContext(WorkspaceContext);if (!value) throw new Error('Workspace is unavailable.');return value;}
export function useCopy() {const {locale}=usePreferences();return (id:string,en:string) => locale==='id'?id:en;}

export function useResource<T>(path:string,decode:Decoder<T>) {
  const {api,stamp}=useWorkspace();const [revision,setRevision]=useState(0);
  const identity=`${stamp.generation}:${stamp.binding}:${path}:${revision}`;
  const [state,setState]=useState<{identity:string;data?:T;error?:ApiFailure}>({identity:''});
  useEffect(() => {
    const controller=new AbortController();let current=true;
    void api.resourceRead({path,decode},stamp,controller.signal).then((data) => {
      if (current) setState({identity,data});
    }).catch((error:unknown) => {if (current) setState({identity,error:error instanceof ApiFailure?error:new ApiFailure('unavailable')});});
    return () => {current=false;controller.abort();};
  },[api,stamp,path,decode,identity]);
  const current=state.identity===identity?state:undefined;
  const refresh=useCallback(() => setRevision((value) => value+1),[]);
  return {data:current?.data,error:current?.error,loading:!current,refresh};
}

// A resource owns one unresolved intent. Leaving/re-entering the view never
// manufactures another key; only the explicit reconciliation action can replay.
export function useWrite<T>(method:JsonWrite['method'],path:string,decode:Decoder<T>) {
  const {api,stamp}=useWorkspace();const mounted=useRef(true);const busyRef=useRef(false);
  const [busy,setBusy]=useState(false);const [error,setError]=useState<ApiFailure>();
  useSyncExternalStore(api.intents.subscribe,api.intents.getSnapshot);
  useEffect(() => {mounted.current=true;return () => {mounted.current=false;};},[]);
  const intent=api.intents.pending(`${method} ${path}`,stamp.generation);
  const unknown=intent?.state==='unknown';
  useUnsavedInput(busy || !!intent);
  async function execute(body?:Record<string,unknown>):Promise<T|undefined> {
    if (busyRef.current) return;
    busyRef.current=true;setBusy(true);setError(undefined);
    try {
      const pending=api.intents.pending(`${method} ${path}`,stamp.generation);
      if (pending && (body!==undefined || pending.state!=='unknown')) throw new ApiFailure('conflict');
      if (!pending && body===undefined) throw new ApiFailure('input');
      const prepared=pending ?? api.prepareWrite({method,path,body:body!},stamp);
      const receipt=await api.dispatchWrite(prepared.key,stamp,decode,{reconcile:!!pending});
      return mounted.current?receipt:undefined;
    } catch (failure) {if (mounted.current) setError(failure instanceof ApiFailure?failure:new ApiFailure('unavailable'));return undefined;}
    finally {busyRef.current=false;if (mounted.current) setBusy(false);}
  }
  return {execute,busy:busy || intent?.state==='dispatching',error,unknown,intent};
}

