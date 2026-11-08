import asyncio
import json
import pytest
from reimbursement_service.api.boundary import RequestBoundary


def test_expiry_after_service_before_headers_suppresses_success_buffer():
    async def run():
        output=[];clock=[100]
        async def downstream(scope,receive,send):
            scope['state']['operation_authority_deadline']=101
            value=b'{"proof":"must_not_be_released"}'
            # The service already constructed a successful result. Authority
            # expires during the subsequent response preparation.
            clock[0]=101
            await send({'type':'http.response.start','status':200,'headers':[(b'content-length',str(len(value)).encode())]})
            await send({'type':'http.response.body','body':value})
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):output.append(message)
        await RequestBoundary(downstream,clock=lambda:clock[0])({'type':'http','headers':[]},receive,send)
        assert output[0]['status']==503
        assert b'must_not_be_released' not in output[1]['body']
        assert json.loads(output[1]['body'])['error']['retryable'] is True
        headers=dict(output[0]['headers'])
        assert int(headers[b'content-length'])==len(output[1]['body'])
    asyncio.run(run())


def test_start_valid_then_body_at_deadline_is_held():
    async def run():
        output=[];clock=[100]
        async def downstream(scope,receive,send):
            scope['state']['operation_authority_deadline']=101
            await send({'type':'http.response.start','status':200,'headers':[]})
            assert output==[]
            clock[0]=101
            await send({'type':'http.response.body','body':b'private-proof'})
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):output.append(message)
        await RequestBoundary(downstream,clock=lambda:clock[0])({'type':'http','headers':[]},receive,send)
        assert output[0]['status']==503 and b'private-proof' not in output[1]['body']
    asyncio.run(run())


def test_expiry_during_header_send_aborts_before_private_body():
    async def run():
        output=[];clock=[100]
        async def downstream(scope,receive,send):
            scope['state']['operation_authority_deadline']=101
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'private-proof'})
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):
            output.append(message)
            if message['type']=='http.response.start':clock[0]=101
        with pytest.raises(RuntimeError):
            await RequestBoundary(downstream,clock=lambda:clock[0])({'type':'http','headers':[]},receive,send)
        assert len(output)==1 and output[0]['status']==200
    asyncio.run(run())


def test_protected_complete_json_buffer_is_bounded():
    async def run():
        output=[]
        async def downstream(scope,receive,send):
            scope['state']['operation_authority_deadline']=101
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'x'*65536,'more_body':True})
            assert output==[]
            await send({'type':'http.response.body','body':b'x','more_body':False})
        async def receive():return {'type':'http.request','body':b''}
        async def send(message):output.append(message)
        await RequestBoundary(downstream,clock=lambda:100)({'type':'http','headers':[]},receive,send)
        assert output[0]['status']==503 and len(output[1]['body'])<1000
    asyncio.run(run())
