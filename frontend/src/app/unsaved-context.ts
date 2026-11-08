import { createContext, useContext, useLayoutEffect, useRef } from 'react';

type DirtyPort = { set: (key: symbol, dirty: boolean) => void };
export const DirtyContext = createContext<DirtyPort | null>(null);
export function useUnsavedInput(dirty: boolean): void {
  const context = useContext(DirtyContext);
  const key = useRef(Symbol('input'));
  useLayoutEffect(() => {
    const id = key.current; context?.set(id, dirty);
    return () => context?.set(id, false);
  }, [context, dirty]);
}
