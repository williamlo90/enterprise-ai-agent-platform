import type { ReactNode } from 'react';
import type { ApiFailure } from '../api/failure';
import { WorkspaceContext } from './workspace-context';
import type { Workspace } from './workspace-context';
import { useCopy } from './workspace-hooks';
import { Button } from '../components/Button';
import { StatusMessage } from '../components/StatusMessage';

export function WorkspaceProvider({api,stamp,children}:Workspace & {children:ReactNode}) {
  return <WorkspaceContext value={{api,stamp}}>{children}</WorkspaceContext>;
}

export function ResourceState({loading,error,retry,retryBlocked=false}:{loading:boolean;error?:ApiFailure;retry:()=>void;retryBlocked?:boolean}) {
  const c=useCopy();
  if (loading) return <StatusMessage kind="loading" title={c('Memuat data','Loading data')} />;
  if (!error) return null;
  return <StatusMessage kind="error" title={error.kind==='not_found'?c('Tidak tersedia','Unavailable'):c('Data belum dapat dimuat','Data could not be loaded')}>
    <p>{error.kind==='not_found'?c('Data tidak tersedia atau akses Anda telah berubah.','The resource is unavailable or your access has changed.'):c('Periksa kembali untuk mengambil data terbaru.','Check again to retrieve current data.')}</p>
    {error.requestId&&<p>{c('ID permintaan: ','Request ID: ')}{error.requestId}</p>}
    <Button disabled={retryBlocked} onClick={retry}>{c('Periksa kembali','Check again')}</Button>
  </StatusMessage>;
}

export function WriteState({error,unknown,busy,reconcile}:{error?:ApiFailure;unknown:boolean;busy:boolean;reconcile:()=>void}) {
  const c=useCopy();
  if (!error && !unknown) return null;
  return <StatusMessage kind="error" title={unknown?c('Hasil penyimpanan belum diketahui','Save result unknown'):error?.kind==='conflict'?c('Data telah berubah','Data has changed'):c('Belum dapat disimpan','Could not save')}>
    <p>{unknown?c('Jangan kirim perubahan baru. Periksa hasil permintaan yang sama terlebih dahulu.','Do not submit a new change. Check the result of the same request first.'):error?.kind==='conflict'?c('Muat ulang data sebelum mengedit kembali. Perubahan belum diterapkan.','Reload current data before editing again. The change was not applied.'):c('Periksa isian dan kewenangan Anda, lalu coba kembali.','Check your input and permissions, then try again.')}</p>
    {unknown && <Button disabled={busy} onClick={reconcile}>{c('Periksa hasil penyimpanan','Check save result')}</Button>}
  </StatusMessage>;
}
