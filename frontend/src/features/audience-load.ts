import { ApiFailure } from '../api/failure';
import type { ApiCoordinator,ContextStamp } from '../app/api-coordinator';
import { audienceDecoder,decodeSelectionPage } from './audience-api';
import type { Audience,FrozenSelection,Reader } from './audience-api';

export async function loadAudience(api:ApiCoordinator,stamp:ContextStamp,documentId:string,signal:AbortSignal):Promise<Audience> {
  const decode=audienceDecoder(documentId);let first:Audience|undefined;const items:Reader[]=[];let after:string|null=null;
  for (let page=0;page<20;page++) {
    const row:Audience=await api.resourceRead({path:'/api/v1/knowledge/documents/'+documentId+'/audience?limit=50'+(after?'&after='+after:''),decode},stamp,signal);
    if (signal.aborted) throw new ApiFailure('cancelled');
    if (first && (row.document_version!==first.document_version || row.access_revision!==first.access_revision || row.audience_mode!==first.audience_mode || row.explicit_readers_count!==first.explicit_readers_count)) throw new ApiFailure('conflict');
    first ??=row;
    if (after && row.readers[0] && row.readers[0].principal_id<=after) throw new ApiFailure('malformed');
    items.push(...row.readers);
    if (!row.next_after) {if (items.length!==row.explicit_readers_count) throw new ApiFailure('conflict');return {...row,readers:items,next_after:null};}
    after=row.next_after;
  }
  throw new ApiFailure('input');
}
export async function loadSelection(api:ApiCoordinator,stamp:ContextStamp,receipt:FrozenSelection,signal:AbortSignal):Promise<Reader[]> {
  const items:Reader[]=[];let after:string|null=null;
  for (let page=0;page<20;page++) {
    const row:ReturnType<typeof decodeSelectionPage>=await api.resourceRead({path:'/api/v1/knowledge/selections/'+receipt.selection_id+'?limit=50'+(after?'&after='+after:''),decode:decodeSelectionPage},stamp,signal);
    if (signal.aborted) throw new ApiFailure('cancelled');
    for (const key of Object.keys(receipt) as (keyof FrozenSelection)[]) if (row[key]!==receipt[key]) throw new ApiFailure('conflict');
    if (after && row.items[0] && row.items[0].principal_id<=after) throw new ApiFailure('malformed');
    items.push(...row.items);
    if (!row.next_after) {if (items.length!==receipt.selected_count) throw new ApiFailure('malformed');return items;}
    after=row.next_after;
  }
  throw new ApiFailure('input');
}
