import { ApiFailure } from './failure';
import { isUuid, object } from './transport';

export type UploadMime = 'application/pdf' | 'text/plain' | 'text/markdown' | 'image/jpeg' | 'image/png';
export interface UploadDescriptor { kind:'upload'; path:string; method:'POST'; status:202; mime:UploadMime; collection_id:string|null; size:number; digest:string; purpose?:'knowledge'|'receipt' }
export const maximumUploadBytes=10*1024*1024;
export function uploadPath(collection:string|null,purpose:'knowledge'|'receipt'='knowledge'):string {
  if (!['knowledge','receipt'].includes(purpose) || purpose==='receipt' && collection!==null) throw new ApiFailure('input');
  if (collection !== null && !isUuid(collection)) throw new ApiFailure('input');
  return '/api/v1/files?purpose='+purpose+(collection ? '&collection_id='+collection : '');
}
export function describeUpload(mime:UploadMime,collection:string|null,size:number,digest:string,purpose:'knowledge'|'receipt'='knowledge'):UploadDescriptor {
  if (!(purpose==='receipt'?['application/pdf','image/jpeg','image/png']:['application/pdf','text/plain','text/markdown']).includes(mime) || !Number.isSafeInteger(size) || size<1 || size>maximumUploadBytes || !/^[a-f0-9]{64}$/.test(digest)) throw new ApiFailure('input');
  return { kind:'upload',path:uploadPath(collection,purpose),method:'POST',status:202,mime,collection_id:collection,size,digest,...(purpose==='receipt'?{purpose}: {}) };
}
export function decodeUpload(material:string):UploadDescriptor {
  try {
    const value=object(JSON.parse(material) as unknown);
    if (value.kind!=='upload' || value.method!=='POST' || value.status!==202 || typeof value.mime!=='string' || typeof value.size!=='number' || typeof value.digest!=='string' || value.collection_id!==null && typeof value.collection_id!=='string') throw new ApiFailure('input');
    const result=describeUpload(value.mime as UploadMime,value.collection_id as string|null,value.size,value.digest,(value.purpose ?? 'knowledge') as 'knowledge'|'receipt');
    if (value.path!==result.path) throw new ApiFailure('input');
    return result;
  } catch { throw new ApiFailure('input'); }
}
