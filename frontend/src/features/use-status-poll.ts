import { useEffect,useState } from 'react';
import type { Decoder } from '../api/transport';
import { useWorkspace } from './workspace-hooks';
import { startStatusPoll } from './status-poll';
import type { PollState } from './status-poll';

export function useStatusPoll<T>(path:string,decode:Decoder<T>,terminal:(data:T)=>boolean) {
  const {api,stamp}=useWorkspace();const [revision,setRevision]=useState(0);
  const identity=`${stamp.generation}:${stamp.binding}:${path}:${revision}`;
  const [snapshot,setSnapshot]=useState<{identity:string;state:PollState<T>}>();
  const [clock,setClock]=useState(0);
  useEffect(() => {
    const poll=startStatusPoll((signal) => api.resourceRead({path,decode},stamp,signal),terminal,(state) => setSnapshot({identity,state}));
    const hidden=() => {if (document.visibilityState==='hidden') poll.pause();};
    const offline=() => poll.pause();
    const unsubscribe=api.subscribeInvalidation(poll.pause);
    document.addEventListener('visibilitychange',hidden);window.addEventListener('offline',offline);
    return () => {poll.stop();unsubscribe();document.removeEventListener('visibilitychange',hidden);window.removeEventListener('offline',offline);};
  },[api,stamp,path,decode,terminal,identity]);
  const state:PollState<T>=snapshot?.identity===identity?snapshot.state:{phase:'loading'};
  const retryAt=state.retryAt ?? 0;
  useEffect(() => {if (retryAt) {const timer=setTimeout(() => setClock(Date.now()),Math.max(0,retryAt-Date.now()));return () => clearTimeout(timer);}},[retryAt]);
  const retryBlocked=retryAt>clock;
  return {...state,retryBlocked,refresh:() => {if (Date.now()>=retryAt) setRevision((value) => value+1);}};
}
