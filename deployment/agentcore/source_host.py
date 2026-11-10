"""Temporary serverless source host for a bounded AgentCore deployment exercise.

The independent reimbursement app uses its own restricted database credential.
Its actor-verification HTTP port addresses the platform ASGI application locally;
business authorization and SQL transactions remain owned by their applications.
"""
import asyncio
import json
import os
import time

import httpx
from enterprise_ai_platform.agentcore_tools import handler as gateway_handler
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import load_settings
from enterprise_ai_platform.main import build_app
from enterprise_ai_platform.lambda_api import invoke_asgi
from enterprise_ai_platform.integrations.agentcore.clients import load_environment
from reimbursement_service.config import Settings as ReferenceSettings
from reimbursement_service.main import create_app as reference_app


async def http_request(event, context):
    path = event.get('rawPath', '')
    if path not in {'/service/v1/draft-sources', '/health/live', '/health/ready'}:
        return {'statusCode': 404, 'body': '{"error":"not_found"}'}
    if path == '/health/live':
        return {'statusCode': 200, 'headers': {'content-type': 'application/json'}, 'body': '{"status":"ok"}'}
    settings = load_settings(None)
    internal = build_app(settings, internal=True)
    private = json.loads(os.environ['REFERENCE_CONFIG_JSON'])
    service = reference_app(ReferenceSettings(**private), platform_transport=httpx.ASGITransport(app=internal))
    async with internal.router.lifespan_context(internal):
        deadline = asyncio.get_running_loop().time() + min(25, context.get_remaining_time_in_millis() / 1000 - 3)
        return await invoke_asgi(service, event, settings.reimbursement_origin, work_deadline=deadline)


def handler(event, context):
    if event.get('version') != '2.0' or 'requestContext' not in event:
        return gateway_handler(event, context)
    try:
        load_environment(os.environ['EAI_AGENTCORE_SECRET_ARN'], os.environ.get('AWS_REGION', 'ap-southeast-1'))
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            return runner.run(http_request(event, context))
    except Exception as error:
        print(json.dumps({'event': 'source_held', 'error_type': type(error).__name__,
                          'status': getattr(error, 'status', None)}), flush=True)
        return {'statusCode': 503, 'headers': {'content-type': 'application/json'}, 'body': '{"error":"source_unavailable"}'}
