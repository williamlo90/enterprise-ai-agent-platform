import { ApiFailure } from '../api/failure';

export type PollState<T>={data?:T;error?:ApiFailure;phase:'loading'|'waiting'|'terminal'|'bounded'|'paused'|'error';retryAt?:number};
// One read at a time; no business writes or worker retry. Every start is bounded.
export function startStatusPoll<T>(read:(signal:AbortSignal)=>Promise<T>,terminal:(data:T)=>boolean,
  emit:(state:PollState<T>)=>void,available:()=>boolean=()=>document.visibilityState!=='hidden' && navigator.onLine) {
  const controller=new AbortController();let stopped=false;let timer:ReturnType<typeof setTimeout>|undefined;let attempts=0;
  const stop=() => {stopped=true;clearTimeout(timer);controller.abort();};
  const pause=() => {stop();emit({phase:'paused'});};
  async function tick() {
    if (stopped) return;
    if (!available()) {pause();return;}
    emit({phase:'loading'});
    try {
      const data=await read(controller.signal);
      if (stopped) return;
      if (!available()) {pause();return;}
      attempts++;
      if (terminal(data)) {emit({data,phase:'terminal'});return;}
      if (attempts>=8) {emit({data,phase:'bounded'});return;}
      emit({data,phase:'waiting'});
      timer=setTimeout(() => {void tick();},Math.min(2000*2**(attempts-1),15000));
    } catch (error) {
      if (stopped) return;
      const failure=error instanceof ApiFailure?error:new ApiFailure('unavailable');
      emit({error:failure,phase:'error',retryAt:Date.now()+(failure.retryAfterMs ?? (failure.status===429?15000:0))});
    }
  }
  void tick();return {stop,pause};
}
