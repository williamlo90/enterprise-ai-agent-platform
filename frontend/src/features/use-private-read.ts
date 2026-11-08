import { useEffect,useState } from 'react';
import type { Decoder } from '../api/transport';
import { ApiFailure } from '../api/failure';
import { useWorkspace } from './workspace-hooks';

// No automatic retry, polling, URL navigation, or persistence. Every action has a
// distinct identity so an older result cannot repopulate a newer request.
export function usePrivateRead<T>(path:string|null,decode:Decoder<T>,request:number) {
  const {api,stamp}=useWorkspace();const identity=`${stamp.generation}:${stamp.binding}:${path}:${request}`;
  const [state,setState]=useState<{identity:string;data?:T;error?:ApiFailure;until?:number}>({identity:''});
  const [now,setNow]=useState(0);
  useEffect(() => {
    if (path===null) return;
    let current=true;const controller=new AbortController();
    void api.resourceRead({path,decode},stamp,controller.signal).then((data) => {if (current) setState({identity,data});}).catch((error:unknown) => {
      if (!current) return;const failure=error instanceof ApiFailure?error:new ApiFailure('unavailable');
      setState({identity,error:failure,until:failure.retryAfterMs?Date.now()+failure.retryAfterMs:0});
    });
    return () => {current=false;controller.abort();};
  },[api,stamp,path,decode,request,identity]);
  const current=state.identity===identity?state:undefined;
  const until=current?.until??0;
  useEffect(() => {if (until>0) {const timer=setTimeout(() => setNow(Date.now()),Math.max(0,until-Date.now()));return () => clearTimeout(timer);}},[until]);
  return {data:current?.data,error:current?.error,loading:path!==null && !current,cooling:until>now};
}
