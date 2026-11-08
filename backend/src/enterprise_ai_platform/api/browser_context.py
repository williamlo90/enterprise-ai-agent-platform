"""Strict opt-in rollout, buffered responses and request-owned context binding."""
import re
from starlette.requests import Request
from enterprise_ai_platform.api.identity import cookie, SESSION_COOKIE
from enterprise_ai_platform.api.errors import error_response
from enterprise_ai_platform.modules.identity.service import digest, opaque
from enterprise_ai_platform.modules.identity.ports import BrowserReadScope, browser_read_scope, BrowserContextFailure

EXPECTED = b'x-eai-expected-context'
BINDING = b'x-eai-context-binding'

def mutation_target(method,path):
    if method=='POST':
        if re.fullmatch('/api/v1/privacy/conversations/[^/]+/deletion/execute',path):return 'privacy_conversations','delete'
        if re.fullmatch('/api/v1/privacy/conversations/[^/]+/export',path):return 'privacy_conversations','export'
        if re.fullmatch('/api/v1/privacy/conversations/[^/]+/deletion/cancel',path):return 'privacy_conversations','release'
        if re.fullmatch('/api/v1/privacy/conversations/[^/]+/deletion',path):return 'privacy_conversations','fence'
        if path=='/api/v1/operator/privacy-effect-policy':return 'privacy_effect','publish'
        if path=='/api/v1/operator/privacy-policy':return 'privacy','publish'
        if path=='/api/v1/admin/memberships':return 'administration','member_create'
        if re.fullmatch('/api/v1/admin/memberships/[^/]+/roles',path):return 'administration','role'
        if path=='/api/v1/operator/tenants':return 'administration','provision'
        if re.fullmatch('/api/v1/conversations/[^/]+/workflow',path):return 'conversations','assisted_append'
        if re.fullmatch('/api/v1/runs/[^/]+/resume',path):return 'conversations','resume'
        if re.fullmatch('/api/v1/drafts/[^/]+/confirm',path):return 'actions','confirm'
        if re.fullmatch('/api/v1/claims/[^/]+/decisions',path):return 'actions','decision'
        if path=='/api/v1/drafts':return 'drafts','create'
        if re.fullmatch('/api/v1/drafts/[^/]+/preview',path):return 'drafts','preview'
        policy=re.fullmatch('/api/v1/policy-packages/[^/]+/versions/[^/]+/(draft|prepare|dispatch|activate|supersede|retire|withdraw|conflict)',path)
        if policy:return 'policies',('ack' if policy.group(1)=='dispatch' else policy.group(1))
        if path=='/api/v1/files':return 'files','status'
        if path=='/api/v1/knowledge/documents':return 'knowledge','create'
        if path=='/api/v1/knowledge/selections':return 'knowledge','selection_create'
        if re.fullmatch('/api/v1/knowledge/documents/[^/]+/versions',path):return 'knowledge','version'
        if re.fullmatch('/api/v1/knowledge/versions/[^/]+/publish',path):return 'knowledge','publish'
        if re.fullmatch('/api/v1/knowledge/documents/[^/]+/withdraw',path):return 'knowledge','withdraw'
        if re.fullmatch('/api/v1/runs/[^/]+/recover',path):return 'conversations','recover'
        if path=='/api/v1/conversations':return 'conversations','create'
        if re.fullmatch('/api/v1/conversations/[^/]+/messages',path):return 'conversations','append'
        if re.fullmatch('/api/v1/messages/[^/]+/feedback',path):return 'conversations','feedback'
    if method=='PATCH':
        if path=='/api/v1/admin/config':return 'administration','config_write'
        if re.fullmatch('/api/v1/admin/memberships/[^/]+',path):return 'administration','member_active'
        if re.fullmatch('/api/v1/drafts/[^/]+',path):return 'drafts','replace'
        if re.fullmatch('/api/v1/organization/members/[^/]+',path):return 'organization','replace'
        if re.fullmatch('/api/v1/knowledge/documents/[^/]+/audience',path):return 'knowledge','audience_replace'
        if re.fullmatch('/api/v1/knowledge/documents/[^/]+/acl',path):return 'knowledge','acl'
        if re.fullmatch('/api/v1/conversations/[^/]+/name',path):return 'conversations','rename'
    return None


def read_target(path):
    if re.fullmatch('/api/v1/privacy/conversations/[^/]+/deletion',path):return 'privacy_conversations','fence_status'
    if path=='/api/v1/operator/privacy-effect-policy':return 'privacy_effect','current'
    if re.fullmatch('/api/v1/privacy/conversations/[^/]+/inventory',path):return 'privacy_conversations','inventory'
    if path=='/api/v1/operator/privacy-policy':return 'privacy','current'
    admin_paths={'/api/v1/admin/memberships':'members','/api/v1/admin/config':'config','/api/v1/admin/config/catalog':'catalog','/api/v1/tenant/profile':'profile','/api/v1/admin/scopes':'scopes','/api/v1/admin/policies':'policies','/api/v1/operator/tenants':'tenants','/api/v1/admin/jobs':'jobs','/api/v1/operator/jobs':'jobs','/api/v1/admin/audit':'audit'}
    if path in admin_paths:return 'administration',admin_paths[path]
    if re.fullmatch('/api/v1/admin/memberships/[^/]+',path):return 'administration','member'
    if re.fullmatch('/api/v1/admin/policies/[^/]+/versions/[^/]+',path):return 'administration','policy'
    if re.fullmatch('/api/v1/drafts/[^/]+/rules',path):return 'workspace','rules'
    if path in {'/api/v1/drafts','/api/v1/actions'}:return 'workspace',path.rsplit('/',1)[-1]
    if path in {'/api/v1/claims','/api/v1/approvals'} or re.fullmatch('/api/v1/(claims|approvals|trips)/[^/]+',path):return 'workspace','workspace_read'
    if re.fullmatch('/api/v1/actions/[^/]+',path):return 'actions','read'
    if re.fullmatch('/api/v1/drafts/[^/]+',path):return 'drafts','read'
    if path=='/api/v1/trips':return 'trips','list_recheck'
    if path.startswith('/api/v1/organization/'):
        return 'organization',('facets' if path.endswith('/facets') else 'directory' if path.endswith('/members') else 'member')
    if path == '/api/v1/knowledge/directory':return 'organization','directory'
    if path == '/api/v1/knowledge/directory/facets':return 'organization','facets'
    if path == '/api/v1/knowledge/collections':return 'organization','collections'
    if path.startswith('/api/v1/files/'):
        return 'files', 'download' if path.endswith('/content') else 'status'
    if path.startswith('/api/v1/knowledge/'):
        if path.endswith('/audience'):return 'knowledge','audience'
        if re.fullmatch('/api/v1/knowledge/selections/[^/]+',path):return 'knowledge','selection'
        if path == '/api/v1/knowledge/documents':return 'knowledge','catalog'
        if re.fullmatch('/api/v1/knowledge/documents/[^/]+',path):return 'knowledge','detail'
        if re.fullmatch('/api/v1/knowledge/documents/[^/]+/versions',path):return 'knowledge','versions'
        if path.endswith('/preview'):return 'knowledge','preview_recheck'
        return 'knowledge', ('search_recheck' if path.endswith('/search') else 'source_recheck' if path.endswith('/content') else 'status')
    if path == '/api/v1/conversations' or path.startswith('/api/v1/conversations/'):
        return 'conversations', 'history_recheck' if path.endswith('/messages') else 'list'
    if re.fullmatch('/api/v1/runs/[^/]+/workflow',path):return 'conversations','assisted_read'
    if path.startswith('/api/v1/runs/'):
        return 'conversations', 'diagnostics' if path.endswith('/diagnostics') else 'read'
    if path.startswith('/api/v1/messages/'):
        return 'conversations', 'unimplemented'
    return None


class BrowserContextBoundary:
    def __init__(self, app, enabled=False):
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        values = [v.decode('latin1') for k,v in scope.get('headers',[]) if k.lower() == EXPECTED]
        request_id = scope.get('state',{}).get('request_id','')
        async def reject(status):
            await error_response(status, request_id)(scope, receive, send)
        if not self.enabled:
            if values:
                return await reject(503)
            return await self.app(scope, receive, send)
        path, method = scope.get('path',''), scope.get('method','')
        target = read_target(path) if method == 'GET' else mutation_target(method,path)
        bootstrap = (path == '/api/v1/me' and method == 'GET') or (path == '/api/v1/session/tenant' and method == 'POST')
        if not target and not bootstrap:
            if values:return await reject(400)
            return await self.app(scope, receive, send)
        if target and (len(values) != 1 or not re.fullmatch('[a-f0-9]{64}',values[0])):
            return await reject(400)
        session = cookie(Request(scope), SESSION_COOKIE)
        if not opaque(session):
            return await reject(401)
        context = BrowserReadScope('read' if target else 'bootstrap', digest('session',session),
            expected=values[0] if target else None, family=target[0] if target else None,
            final_operation=target[1] if target else None)
        marker = browser_read_scope.set(context)
        pending = None
        rejected = False
        async def guarded_send(message):
            nonlocal pending, rejected
            if rejected:
                return
            if message['type'] == 'http.response.start' and 200 <= message['status'] < 300:
                pending = message
                return
            if pending is not None and message['type'] == 'http.response.body':
                try:
                    if message.get('more_body'):
                        raise BrowserContextFailure()
                    binding = context.response_binding()
                except BrowserContextFailure as error:
                    rejected = True
                    return await reject(error.status)
                headers = [(k,v) for k,v in pending.get('headers',[]) if k.lower() != BINDING]
                if target:
                    headers.append((BINDING,binding.encode('ascii')))
                await send({**pending,'headers':headers})
                pending = None
            await send(message)
        try:
            await self.app(scope, receive, guarded_send)
        finally:
            context.active = False
            context.attestation = None
            browser_read_scope.reset(marker)
