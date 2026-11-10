"""AgentCore HTTP Runtime. The existing LangGraph turns retain business authority."""
import asyncio
from contextlib import asynccontextmanager
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langgraph.types import Command
from enterprise_ai_platform.composition import build_resources, build_rag_turn, build_assisted_turn, build_model_adapters, build_workflow_model
from enterprise_ai_platform.config import load_settings
from enterprise_ai_platform.agentcore_execution import bound_job
from enterprise_ai_platform.integrations.agentcore.clients import Gateway, GatewayRepository, ExecutionMemory, load_environment, phase
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.integrations.checkpoints.assisted_graph import build_assisted_graph


@asynccontextmanager
async def lifespan(app):
    region = os.environ.get('AWS_REGION', 'ap-southeast-1')
    if region != 'ap-southeast-1':
        raise ValueError('Use the configured Singapore integration.')
    load_environment(os.environ['EAI_AGENTCORE_SECRET_ARN'], region)
    resources = build_resources(load_settings(None))
    resources.background_refresh = False
    async with resources.lifecycle():
        app.state.resources = resources
        app.state.execution = asyncio.Lock()
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get('/ping')
async def ping():
    return {'status': 'Healthy'}


@app.post('/invocations')
async def invoke(request: Request):
    chunks = bytearray()
    async for part in request.stream():
        chunks.extend(part)
        if len(chunks) > 4096:
            return JSONResponse({'error': 'invalid_invocation'}, status_code=400)
    gateway = memory = None
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate field.')
                result[key] = value
            return result
        value = json.loads(chunks, object_pairs_hook=unique)
        if type(value) is not dict or set(value) != {'claim'}:
            raise ValueError('Invalid invocation.')
        if request.app.state.execution.locked():
            return JSONResponse({'error': 'session_busy'}, status_code=409)
        async with request.app.state.execution:
            resources = request.app.state.resources
            claim, jobs, lease, ref, route, binding = await bound_job(resources, value['claim'])
            if os.environ.get('EAI_ENABLE_MODEL_EXECUTION') != 'true' or os.environ.get('EAI_FICTIONAL_APAC_ENABLED') != 'true':
                raise ValueError('Model execution not enabled.')
            region = os.environ.get('AWS_REGION', 'ap-southeast-1')
            gateway = Gateway(os.environ['EAI_AGENTCORE_GATEWAY_URL'], region)
            await asyncio.to_thread(gateway.initialize)
            with phase('agentcore.' + route['agent_kind']) as span:
                span.set_attribute('eai.job_id', claim['id'])
                if route['agent_kind'] == 'assisted':
                    turn = await build_assisted_turn(resources.database, jobs, claim, source=resources.drafts.source,
                        model_factory=lambda before: build_workflow_model(before, fictional_apac=True))
                    turn.repository = GatewayRepository(turn.repository, gateway, claim, assisted=True)
                else:
                    turn = await build_rag_turn(resources.database, jobs, claim,
                        model_factory=lambda before: build_model_adapters(before, fictional_apac=True))
                    turn.repository = GatewayRepository(turn.repository, gateway, claim)
                # Construction validates the source-owned tenant/run/lease before any Memory access.
                memory = ExecutionMemory(os.environ['EAI_AGENTCORE_MEMORY_ID'], region, claim['tenant_id'], ref)
                try:
                    recent = await asyncio.to_thread(memory.recent)
                    span.set_attribute('eai.execution_context_available', bool(recent))
                except Exception:
                    span.set_attribute('eai.memory_read', 'unavailable')
                if route['agent_kind'] == 'assisted':
                    result = await turn.run(build_assisted_graph, resume_input=Command(resume={'continue': True}) if route.get('resuming') else None)
                    outcome = 'waiting' if result.get('__interrupt__') else 'completed'
                else:
                    await turn.run(build_rag_graph)
                    outcome = 'completed'
                try:
                    await asyncio.to_thread(memory.record, outcome)
                except Exception:
                    # A failed context write must never retry an already-committed business step.
                    span.set_attribute('eai.memory_write', 'unavailable')
                return {'status': 'ok', 'job_id': claim['id'], 'generation': claim['generation'], 'state': outcome}
    except Exception:
        return JSONResponse({'error': 'execution_held'}, status_code=503)
    finally:
        if gateway:
            gateway.close()
        if memory:
            memory.close()
