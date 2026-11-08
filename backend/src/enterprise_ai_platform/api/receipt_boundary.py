"""Hold small private receipt responses until their final authority check."""
import time
from enterprise_ai_platform.api.errors import error_response


class ReceiptResponseBoundary:
    def __init__(self,app,*,clock=time.time):self.app,self.clock=app,clock

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        pending=None;body=bytearray();finished=False
        path=scope.get('path','')
        limit=10485760 if path.startswith('/api/v1/files/') and '/claims/' in path and path.endswith('/content') else 32768
        if path.startswith('/api/v1/drafts'):limit=131072
        if path.startswith(('/api/v1/admin/','/api/v1/operator/','/api/v1/tenant/')):limit=262144
        if path.startswith(('/api/v1/claims','/api/v1/approvals','/api/v1/trips')):limit=262144
        async def guarded(message):
            nonlocal pending,finished
            if finished:return
            deadline=scope.get('state',{}).get('privacy_deadline',scope.get('state',{}).get('receipt_authority_deadline',scope.get('state',{}).get('draft_authority_deadline',scope.get('state',{}).get('action_authority_deadline',scope.get('state',{}).get('workspace_authority_deadline',scope.get('state',{}).get('administration_deadline'))))))
            if message['type']=='http.response.start' and 200<=message['status']<300 and deadline is not None:
                pending=message;return
            if message['type']=='http.response.body' and pending is not None:
                chunk=message.get('body',b'')
                if len(body)+len(chunk)>limit or type(deadline) is not int or self.clock()>=deadline:
                    pending=None;body.clear();finished=True
                    return await error_response(503,scope['state']['request_id'])(scope,receive,send)
                body.extend(chunk)
                if message.get('more_body',False):return
                headers=pending;pending=None;finished=True
                await send(headers)
                if self.clock()>=deadline:
                    body.clear();raise RuntimeError('Receipt response authority expired.')
                await send({'type':'http.response.body','body':bytes(body),'more_body':False})
                body.clear();return
            await send(message)
        await self.app(scope,receive,guarded)
