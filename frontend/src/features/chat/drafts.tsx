import {useState} from 'react';
import type {ReactNode} from 'react';
import {useUnsavedInput} from '../../app/unsaved-context';
import {DraftContext} from './use-chat-draft';
// Scoped to the mounted conversation/auth generation. Nothing is persisted.
export function ChatDrafts({children}:{children:ReactNode}) {
  const [values,setValues]=useState<Record<string,string|boolean>>({});
  useUnsavedInput(Object.entries(values).some(([key,value])=>key.endsWith(':editing')&&value===true||key.endsWith(':reason')&&typeof value==='string'&&!!value));
  return <DraftContext value={{values,set:(key,value)=>setValues(previous=>({...previous,[key]:value}))}}>{children}</DraftContext>;
}
