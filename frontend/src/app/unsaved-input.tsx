import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { DirtyContext } from './unsaved-context';
import { useBlocker } from 'react-router';
import { Modal } from '../components/Modal';
import { Button } from '../components/Button';
import { authMessages } from '../locales/auth';
import { usePreferences } from './preference-context';

// Mount only within the current verified context. Invalidating that context unmounts inputs and guards.
export function NavigationBoundary({ children }: { children: (request: (action: () => void) => void) => ReactNode }) {
  const { locale } = usePreferences(); const copy = authMessages[locale];
  const [port] = useState(() => {
    const keys = new Set<symbol>();
    return { keys, set: (key: symbol, dirty: boolean) => { if (dirty) keys.add(key); else keys.delete(key); } };
  });
  const [action, setAction] = useState<(() => void) | null>(null);
  const blocker = useBlocker(({ currentLocation, nextLocation }) => port.keys.size > 0 && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    const leave = (event: BeforeUnloadEvent) => { if (port.keys.size) { event.preventDefault(); event.returnValue = ''; } };
    window.addEventListener('beforeunload', leave); return () => window.removeEventListener('beforeunload', leave);
  }, [port]);
  const request = (next: () => void) => { if (port.keys.size) setAction(() => next); else next(); };
  const cancel = () => { setAction(null); if (blocker.state === 'blocked') blocker.reset(); };
  const confirm = () => {
    port.keys.clear(); const next = action; setAction(null);
    if (blocker.state === 'blocked') blocker.proceed(); else next?.();
  };
  return <DirtyContext value={port}>{children(request)}
    <Modal open={!!action || blocker.state === 'blocked'} title={copy.unsaved} description={copy.unsavedHint} closeLabel={copy.stay} onClose={cancel}>
      <Button variant="danger" onClick={confirm}>{copy.discard}</Button>
    </Modal>
  </DirtyContext>;
}
