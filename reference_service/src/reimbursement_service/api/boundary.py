"""Bounded input with server-generated correlation; no sensitive request logging."""
import asyncio
import time
from uuid import uuid4
from reimbursement_service.api.errors import error_response


class RequestBoundary:
    def __init__(self, app, max_body_bytes=65536, body_timeout=5, clock=time.time):
        self.app, self.max_body_bytes, self.body_timeout = app, max_body_bytes, body_timeout
        self.clock=clock

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        body_limit=self.max_body_bytes
        if scope.get('path')=='/service/v1/claims/execute' and scope.get('method')=='POST':body_limit=min(131072,self.max_body_bytes*2)
        started = False
        pending_start=None
        protected_buffer=bytearray()
        protected_finished=False

        def private_headers(headers):
            values=[(k,v) for k,v in headers if k.lower() not in
                {b'x-request-id',b'cache-control',b'server',b'x-content-type-options',b'referrer-policy'}]
            return values+[(b'x-request-id',request_id.encode()),(b'cache-control',b'no-store'),
                           (b'x-content-type-options',b'nosniff'),(b'referrer-policy',b'no-referrer')]

        async def protected_failure():
            nonlocal started,pending_start,protected_finished
            pending_start=None;protected_buffer.clear();protected_finished=True
            failure=error_response(503,request_id)
            started=True
            await send({'type':'http.response.start','status':503,'headers':private_headers(failure.raw_headers)})
            await send({'type':'http.response.body','body':failure.body,'more_body':False})

        async def safe_send(message):
            nonlocal started,pending_start,protected_finished
            if protected_finished:return
            if message["type"] == "http.response.start":
                deadline=scope.get('state',{}).get('operation_authority_deadline')
                message={**message,'headers':private_headers(message.get('headers',[]))}
                if 200<=message['status']<300 and deadline is not None:
                    pending_start=message
                    return
                pending_start=None;protected_buffer.clear();started=True
            elif message['type']=='http.response.body' and pending_start is not None:
                chunk=message.get('body',b'')
                if len(protected_buffer)+len(chunk)>(262144 if scope.get('path')=='/service/v1/workspace' else 65536):
                    await protected_failure();return
                protected_buffer.extend(chunk)
                if message.get('more_body',False):return
                deadline=scope['state']['operation_authority_deadline']
                if self.clock()>=deadline:
                    await protected_failure();return
                headers=pending_start;pending_start=None;started=True
                await send(headers)
                # Sending headers can suspend. Do not release a private buffer
                # if authority expired during that await; abort the response so
                # the caller treats the outcome as unknown and looks it up.
                if self.clock()>=deadline:
                    protected_buffer.clear();protected_finished=True
                    raise RuntimeError('Operation response authority expired.')
                protected_finished=True
                await send({'type':'http.response.body','body':bytes(protected_buffer),'more_body':False})
                protected_buffer.clear()
                return
            await send(message)

        async def reject(status):
            await error_response(status, request_id)(scope, receive, safe_send)

        try:
            lengths = [v for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
            if lengths and (len(lengths) != 1 or not lengths[0] or len(lengths[0]) > 20
                            or any(x < 48 or x > 57 for x in lengths[0])):
                await reject(400)
                return
            expected = int(lengths[0]) if lengths else None
            if expected is not None and expected > body_limit:
                await reject(413)
                return
            body = bytearray()
            try:
                async with asyncio.timeout(self.body_timeout):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        if event["type"] != "http.request":
                            await reject(400)
                            return
                        chunk = event.get("body", b"")
                        if len(body) + len(chunk) > body_limit:
                            await reject(413)
                            return
                        body.extend(chunk)
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                await reject(408)
                return
            if expected is not None and expected != len(body):
                await reject(400)
                return
            delivered = False

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, replay, safe_send)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not started:
                await reject(500)
            else:
                raise RuntimeError("Service response interrupted.") from None
