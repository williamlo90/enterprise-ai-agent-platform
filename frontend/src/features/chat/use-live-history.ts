import {useCallback,useEffect,useRef,useState} from 'react';
import {ApiFailure} from '../../api/failure';
import type {Decoder} from '../../api/transport';
import {useWorkspace} from '../workspace-hooks';

interface Snapshot<T> {scope:string;data?:T;error?:ApiFailure;loading:boolean;masked:boolean;cycle:number;retryAt:number}
// A full visible page is revalidated, never merely its last sequence. One cycle
// at a time; lifecycle bursts coalesce, and a cancelled cycle cannot restore data.
export function useLiveHistory<T>(path:string,decode:Decoder<T>) {
  const {api,stamp}=useWorkspace();const scope=stamp.generation+':'+stamp.binding+':'+path;
  const [state,setState]=useState<Snapshot<T>>();const [clock,setClock]=useState(0);
  const refreshRef=useRef<()=>void>(()=>{});
  useEffect(()=>{
    let alive=true,busy=false,again=false,cycle=0,retryAt=0,retained:T|undefined;
    let controller:AbortController|undefined,timer:ReturnType<typeof setTimeout>|undefined,slow:ReturnType<typeof setTimeout>|undefined;
    const available=()=>document.visibilityState!=='hidden'&&navigator.onLine;
    const emit=(value:Omit<Snapshot<T>,'scope'|'cycle'|'retryAt'>)=>{if(alive)setState({...value,scope,cycle,retryAt});};
    const pause=()=>{again=false;clearTimeout(timer);clearTimeout(slow);controller?.abort();retained=undefined;emit({loading:false,masked:true});};
    async function read(mask=true) {
      if(!alive||!available()||Date.now()<retryAt)return;
      clearTimeout(timer);
      if(busy){again=true;if(mask){retained=undefined;emit({loading:true,masked:true});controller?.abort();}return;}
      busy=true;again=false;cycle++;const current=new AbortController();controller=current;
      if(mask)retained=undefined;emit({data:retained,loading:true,masked:!retained});
      slow=setTimeout(()=>{retained=undefined;emit({loading:true,masked:true});},2000);
      try{
        const data=await api.resourceRead({path,decode},stamp,current.signal);
        if(!alive||current.signal.aborted||!available())return;
        retained=data;retryAt=0;emit({data,loading:false,masked:false});
        timer=setTimeout(()=>{void read(false);},10000);
      }catch(error){
        if(!alive||current.signal.aborted)return;
        const failure=error instanceof ApiFailure?error:new ApiFailure('unavailable');retained=undefined;
        retryAt=Date.now()+(failure.retryAfterMs??(failure.status===429?15000:0));emit({error:failure,loading:false,masked:true});
      }finally{
        clearTimeout(slow);busy=false;
        if(alive&&again&&available()){again=false;queueMicrotask(()=>{void read();});}
      }
    }
    const refresh=()=>{void read();};
    refreshRef.current=refresh;
    const unsubscribe=api.subscribeInvalidation(pause);void read();
    return()=>{alive=false;clearTimeout(timer);clearTimeout(slow);controller?.abort();unsubscribe();};
  },[api,stamp,path,decode,scope]);
  const current=state?.scope===scope?state:undefined;const retryAt=current?.retryAt??0;
  useEffect(()=>{if(retryAt){const timer=setTimeout(()=>setClock(Date.now()),Math.max(0,retryAt-Date.now()));return()=>clearTimeout(timer);}},[retryAt]);
  const refresh=useCallback(()=>refreshRef.current(),[]);
  return {data:current?.data,error:current?.error,loading:current?.loading??true,masked:current?.masked??true,identity:scope+':'+(current?.cycle??0),cycle:current?.cycle??0,retryBlocked:retryAt>clock,refresh};
}
