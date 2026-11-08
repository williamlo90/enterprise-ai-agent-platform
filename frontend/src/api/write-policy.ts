import { ApiFailure } from './failure';
import { object } from './transport';

export interface JsonWrite { path: string; method: 'POST' | 'PATCH'; body: Record<string, unknown> }
export interface WriteDescriptor extends JsonWrite { kind: 'json'; status: 200 | 201 | 202 }
const id = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
export function writeStatus(method: string, path: string): 200 | 201 | 202 {
  if (method === 'POST') {
    if (path==='/api/v1/admin/memberships' || path==='/api/v1/operator/tenants') return 201;
    if (new RegExp(`^/api/v1/admin/memberships/${id}/roles$`).test(path) || new RegExp(`^/api/v1/policy-packages/${id}/versions/[1-9][0-9]*/(?:draft|prepare|dispatch|activate|supersede|retire|withdraw|conflict)$`).test(path)) return 200;
    if (new RegExp(`^/api/v1/claims/${id}/decisions$`).test(path)) return 202;
    if (path==='/api/v1/drafts' || new RegExp(`^/api/v1/drafts/${id}/preview$`).test(path)) return 200;
    if (new RegExp(`^/api/v1/drafts/${id}/confirm$`).test(path)) return 202;
    if (new RegExp(`^/api/v1/runs/${id}/recover$`).test(path)) return 200;
    if (new RegExp(`^/api/v1/runs/${id}/resume$`).test(path)) return 202;
    if (path === '/api/v1/conversations' || path === '/api/v1/knowledge/selections') return 201;
    if (new RegExp(`^/api/v1/conversations/${id}/(?:messages|workflow)$`).test(path)) return 202;
    if (new RegExp(`^/api/v1/messages/${id}/feedback$`).test(path)) return 200;
    if (path === '/api/v1/knowledge/documents' || new RegExp(`^/api/v1/knowledge/documents/${id}/versions$`).test(path)) return 202;
    if (new RegExp(`^/api/v1/knowledge/(?:versions/${id}/publish|documents/${id}/withdraw)$`).test(path)) return 200;
  }
  if (method==='PATCH' && (path==='/api/v1/admin/config' || new RegExp(`^/api/v1/admin/memberships/${id}$`).test(path))) return 200;
  if (method==='PATCH' && new RegExp(`^/api/v1/drafts/${id}$`).test(path)) return 200;
  if (method === 'PATCH' && new RegExp(`^/api/v1/(?:organization/members/${id}|knowledge/documents/${id}/(?:audience|acl)|conversations/${id}/name)$`).test(path)) return 200;
  throw new ApiFailure('input');
}
export function describeWrite(request: JsonWrite): WriteDescriptor {
  try {
    const body = object(JSON.parse(JSON.stringify(request.body)) as unknown);
    if (['key','client_message_id','csrf_token','csrf_hash','authorization'].some((key) => Object.hasOwn(body,key))) throw new ApiFailure('input');
    return { kind: 'json', path: request.path, method: request.method, body, status: writeStatus(request.method,request.path) };
  } catch { throw new ApiFailure('input'); }
}
export function writeKeyField(method:string,path:string):'key'|'client_message_id'|null {
  if(method==='POST'&&new RegExp(`^/api/v1/drafts/${id}/preview$`).test(path))return null;
  if(method==='POST'&&new RegExp(`^/api/v1/runs/${id}/(?:recover|resume)$`).test(path))return null;
  return method==='POST'&&new RegExp(`^/api/v1/conversations/${id}/(?:messages|workflow)$`).test(path)?'client_message_id':'key';
}
export function decodeWrite(material: string): WriteDescriptor {
  try {
    const value = object(JSON.parse(material) as unknown);
    if (value.kind !== 'json' || typeof value.path !== 'string' || !['POST','PATCH'].includes(String(value.method))) throw new ApiFailure('input');
    const result = describeWrite({ path: value.path, method: value.method as JsonWrite['method'], body: object(value.body) });
    if (result.status !== value.status) throw new ApiFailure('input');
    return result;
  } catch { throw new ApiFailure('input'); }
}
