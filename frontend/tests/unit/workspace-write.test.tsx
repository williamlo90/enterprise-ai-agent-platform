import { useState } from 'react';
import { act,render,screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect,it } from 'vitest';
import { ApiCoordinator } from '../../src/app/api-coordinator';
import { ApiTransport,object } from '../../src/api/transport';
import { WorkspaceProvider } from '../../src/features/workspace';
import { useWrite } from '../../src/features/workspace-hooks';
import { fixture,json } from './api-fixtures';

function Writer() {
  const write=useWrite('POST','/api/v1/conversations',object);
  return <><p>{write.busy?'Busy':write.unknown?'Unknown':'Ready'}</p>
    <button disabled={write.busy || write.unknown} onClick={() => {void write.execute({language:'id'});}}>Save</button>
    {write.unknown && <button onClick={() => {void write.execute();}}>Reconcile</button>}</>;
}
function Harness() {const [visible,setVisible]=useState(true);return <><button onClick={() => setVisible(!visible)}>Toggle view</button>{visible && <Writer />}</>;}

for (const mode of ['lost','timeout'] as const) it('subscribes re-entered write views to late '+mode+' outcome without generating another key',async () => {
  let reject!:()=>void;let writes=0;let tail:Promise<unknown>=Promise.resolve();
  const api=new ApiCoordinator(new ApiTransport(async (path) => {
    if (path.endsWith('/me')) return json(fixture());writes++;
    return new Promise<Response>((_resolve,fail) => {reject=() => fail(new TypeError('private'));});
  }),{request:async (_name,_options,work) => {const next=tail.then(work);tail=next.catch(() => {});return next;}},mode==='timeout'?500:10_000);
  try {
    const stamp=await api.bootstrap();render(<WorkspaceProvider api={api} stamp={stamp}><Harness /></WorkspaceProvider>);
    const user=userEvent.setup();await user.click(screen.getByRole('button',{name:'Save'}));await screen.findByText('Busy');
    await user.click(screen.getByRole('button',{name:'Toggle view'}));await user.click(screen.getByRole('button',{name:'Toggle view'}));
    expect(screen.getByText('Busy')).toBeTruthy();expect((screen.getByRole('button',{name:'Save'}) as HTMLButtonElement).disabled).toBe(true);
    if (mode==='lost') await act(async () => reject());
    await screen.findByRole('button',{name:'Reconcile'});expect(screen.getByText('Unknown')).toBeTruthy();expect(writes).toBe(1);
    expect(api.identityView(stamp).active_tenant_id).toBe(fixture().active_tenant_id);
    if (mode==='timeout') await act(async () => reject());
  } finally {api.dispose();}
});
