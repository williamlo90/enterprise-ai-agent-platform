import { tenant } from './api-fixtures';
export const versionId='88888888-8888-4888-8888-888888888888';
export const sourceChunk={chunk_no:0,page:1,start:0,end:18,text:'😀 Biaya perjalanan',text_digest:'b'.repeat(64)};
export const sourceHit={...sourceChunk,document_id:tenant,title:'Kebijakan perjalanan',version_id:versionId,version_no:1,access_revision:1,index_generation:1,content_digest:'c'.repeat(64),publication_state:'published'};
export const sourceValue={...sourceHit,file_digest:'d'.repeat(64),embedding_model:'cohere.embed-multilingual-v3',parser_ref:'fixture:utf8-v1',chunk_config:'utf8-384-nooverlap-v1',format:'extracted_chunks',valid_from:null,valid_until:null,chunks:[sourceChunk]};
