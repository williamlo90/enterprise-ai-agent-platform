import {createContext,useContext,useState} from 'react';
interface DraftStore {values:Record<string,string|boolean>;set:(key:string,value:string|boolean)=>void}
export const DraftContext=createContext<DraftStore|null>(null);
export function useChatDraft(key:string,initial:string):[string,(value:string)=>void];
export function useChatDraft(key:string,initial:boolean):[boolean,(value:boolean)=>void];
export function useChatDraft(key:string,initial:string|boolean):unknown {
  const store=useContext(DraftContext);const [local,setLocal]=useState(initial);
  return store?[(store.values[key]??initial),(value:string|boolean)=>store.set(key,value)]:[local,setLocal];
}
