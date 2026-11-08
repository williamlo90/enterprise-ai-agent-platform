import { useEffect,useRef,useState,useSyncExternalStore } from 'react';
import { ApiFailure } from '../api/failure';
import type { JsonWrite } from '../api/write-policy';
import { useUnsavedInput } from '../app/unsaved-context';
import { knowledgeExecute,knowledgePending } from './knowledge-operations';
import type { KnowledgeOperationContext } from './knowledge-lifecycle-api';
import { useWorkspace } from './workspace-hooks';

export function useKnowledgeOperation(documentId:string|null) {
  const {api,stamp}=useWorkspace();const mounted=useRef(false);const active=useRef(false);const [busy,setBusy]=useState(false);const [error,setError]=useState<ApiFailure>();
  useSyncExternalStore(api.intents.subscribe,api.intents.getSnapshot);
  const pending=knowledgePending(api,stamp).find((row) => row.entry.context.documentId===documentId);
  useEffect(() => {mounted.current=true;return () => {mounted.current=false;};},[]);
  useUnsavedInput(busy || !!pending);
  async function execute(request?:JsonWrite,context?:KnowledgeOperationContext) {
    if (active.current) return;
    active.current=true;setBusy(true);setError(undefined);
    try {
      if (pending && request) throw new ApiFailure('conflict');
      const receipt=await knowledgeExecute(api,stamp,request,context,pending?.entry.key);
      return mounted.current?receipt:undefined;
    } catch (failure) {if (mounted.current) setError(failure instanceof ApiFailure?failure:new ApiFailure('unavailable'));return undefined;}
    finally {active.current=false;if (mounted.current) setBusy(false);}
  }
  return {pending,busy:busy || pending?.intent.state==='dispatching',unknown:pending?.intent.state==='unknown',error,execute};
}
