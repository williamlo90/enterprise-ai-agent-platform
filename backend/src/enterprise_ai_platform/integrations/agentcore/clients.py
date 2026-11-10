"""Bounded AWS transports; execution authority never comes from model arguments."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
import httpx
from opentelemetry import trace


SDK_CONFIG = Config(connect_timeout=4, read_timeout=120, retries={'total_max_attempts': 1}, max_pool_connections=2)
TRACER = trace.get_tracer('enterprise-ai.agentcore')


@contextmanager
def phase(name):
    # No automatic capture of questions, credentials, results, or source text.
    with TRACER.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except BaseException:
            span.set_attribute('eai.outcome', 'held')
            raise


def load_environment(secret_arn, region):
    """Runtime-only secret loading. Never imports an operator migration credential."""
    import os
    client = boto3.client('secretsmanager', region_name=region, config=SDK_CONFIG)
    try:
        values = json.loads(client.get_secret_value(SecretId=secret_arn)['SecretString'])
    finally:
        client.close()
    if type(values) is not dict or any(
        type(k) is not str or type(v) is not str or
        not (k.startswith(('APP_', 'OIDC_', 'REIMBURSEMENT_')) or k in {'DATABASE_URL', 'PUBLIC_ORIGIN', 'EXECUTORS_JSON', 'REFERENCE_CONFIG_JSON'}) or
        k in {'DATABASE_DIRECT_URL', 'REIMBURSEMENT_MIGRATION_DATABASE_URL'}
        for k, v in values.items()
    ):
        raise ValueError('Invalid runtime secret configuration.')
    os.environ.update(values)


class Gateway:
    def __init__(self, url, region, target='platform', *, session=None):
        parsed = urlsplit(url)
        if (region != 'ap-southeast-1' or parsed.scheme != 'https' or parsed.path != '/mcp'
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or not re.fullmatch(r'[a-z0-9-]+\.gateway\.bedrock-agentcore\.ap-southeast-1\.amazonaws\.com', parsed.hostname or '')
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,40}', target)):
            raise ValueError('Invalid AgentCore gateway configuration.')
        self.url, self.region, self.target = url, region, target
        self.session = session or boto3.Session(region_name=region)
        self.http = httpx.Client(timeout=httpx.Timeout(35, connect=4), follow_redirects=False, trust_env=False)
        self.mcp_session = None

    def close(self):
        self.http.close()

    def rpc(self, method, params, *, notification=False):
        credentials = self.session.get_credentials()
        if credentials is None:
            raise ValueError('AWS runtime credentials unavailable.')
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            message['id'] = str(uuid4())
        body = json.dumps(message, separators=(',', ':'), allow_nan=False)
        if len(body.encode()) > 131072:
            raise ValueError('Gateway request too large.')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': '2025-03-26'}
        if self.mcp_session:
            headers['Mcp-Session-Id'] = self.mcp_session
        request = AWSRequest(method='POST', url=self.url, data=body, headers=headers)
        SigV4Auth(credentials.get_frozen_credentials(), 'bedrock-agentcore', self.region).add_auth(request)
        with self.http.stream('POST', self.url, content=body, headers=dict(request.headers)) as response:
            if response.status_code not in (200, 202, 204):
                raise ValueError('Gateway request unavailable.')
            self.mcp_session = response.headers.get('Mcp-Session-Id', self.mcp_session)
            raw = bytearray()
            for part in response.iter_bytes():
                raw.extend(part)
                if len(raw) > 262144:
                    raise ValueError('Gateway response too large.')
            if notification:
                return None
            if response.headers.get('content-type', '').startswith('text/event-stream'):
                frames = [json.loads(line[5:].strip()) for line in raw.decode().splitlines() if line.startswith('data:')]
                matches = [item for item in frames if item.get('id') == message['id']]
                if len(matches) != 1:
                    raise ValueError('Invalid gateway response.')
                result = matches[0]
            else:
                result = json.loads(raw)
        if result.get('id') != message['id'] or 'error' in result or 'result' not in result:
            raise ValueError('Gateway operation failed.')
        return result['result']

    def initialize(self):
        result = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                        'clientInfo': {'name': 'enterprise-ai-platform', 'version': '1.0'}})
        if result.get('protocolVersion') not in {'2025-03-26', '2025-06-18', '2024-11-05'}:
            raise ValueError('Unsupported MCP protocol.')
        self.rpc('notifications/initialized', {}, notification=True)

    def call(self, tool, binding, arguments):
        if tool not in {'search_policy', 'get_policy_version', 'create_reimbursement_draft'}:
            raise ValueError('Tool is not permitted.')
        with phase('gateway.' + tool):
            result = self.rpc('tools/call', {'name': self.target + '___' + tool,
                                           'arguments': {'binding': binding, 'arguments': arguments}})
        if result.get('isError'):
            raise ValueError('Gateway tool held.')
        value = result.get('structuredContent')
        if value is None:
            blocks = result.get('content', [])
            if len(blocks) != 1 or blocks[0].get('type') != 'text':
                raise ValueError('Invalid tool result.')
            value = json.loads(blocks[0]['text'])
        if type(value) is not dict or value.get('status') != 'ok':
            raise ValueError('Tool authority unavailable.')
        return value


class GatewayRepository:
    """Keep original RPC/settlement/checkpoint ports; route only narrow tools to Gateway."""
    def __init__(self, repository, gateway, claim, *, assisted=False):
        self.repository, self.gateway, self.claim, self.assisted = repository, gateway, claim, assisted

    def __getattr__(self, name):
        return getattr(self.repository, name)

    async def call(self, operation, payload):
        tool = ({'create': 'create_reimbursement_draft', 'replace': 'create_reimbursement_draft',
                 'preview': 'create_reimbursement_draft'} if self.assisted else
                {'search': 'search_policy', 'recheck': 'get_policy_version'}).get(operation)
        if not tool:
            return await self.repository.call(operation, payload)
        binding = {**self.claim, 'epoch': self.repository.binding['epoch']}
        args = {'operation': operation, **payload}
        # Rebuild protected source attestations inside the owning Lambda.
        args.pop('source', None)
        # A remote call does not own a local SQL transaction. Keep the local
        # heartbeat free while Gateway/Lambda cold-start or wait for source IO.
        return await asyncio.to_thread(self.gateway.call, tool, binding, args)


class ExecutionMemory:
    """Source-bound short-term execution context. No prompts, policy text, or credentials."""
    def __init__(self, memory_id, region, tenant_id, reference):
        actor = str(reference['actor_id'])
        original_session = str(reference['session_id'])
        self.actor = 'actor-' + hashlib.sha256((tenant_id + ':' + actor).encode()).hexdigest()
        self.session = 'session-' + hashlib.sha256((tenant_id + ':' + actor + ':' + original_session).encode()).hexdigest()
        self.run = str(reference['target_id'])
        self.id = memory_id
        self.client = boto3.client('bedrock-agentcore', region_name=region, config=SDK_CONFIG)

    def recent(self):
        value = self.client.list_events(memoryId=self.id, actorId=self.actor, sessionId=self.session,
                                        maxResults=5, includePayloads=True)
        allowed = []
        for event in value.get('events', []):
            for item in event.get('payload', []):
                record = item.get('blob')
                if isinstance(record, str):
                    if len(record) > 256:
                        continue
                    try:
                        record = json.loads(record)
                    except (ValueError, TypeError):
                        continue
                if type(record) is dict and set(record) == {'run_id', 'phase'} and record['phase'] in {'completed', 'waiting'}:
                    allowed.append(record)
        return allowed

    def record(self, outcome):
        if outcome not in {'completed', 'waiting'}:
            raise ValueError('Invalid memory outcome.')
        return self.client.create_event(memoryId=self.id, actorId=self.actor, sessionId=self.session,
            eventTimestamp=datetime.now(timezone.utc), clientToken=str(uuid4()),
            payload=[{'blob': json.dumps({'run_id': self.run, 'phase': outcome}, separators=(',', ':'))}])

    def close(self):
        self.client.close()


async def invoke_runtime(runtime_arn, region, claim, jobs, lease, *, keep_session=False):
    # Identity/tenant/session come from the durable job, not the browser payload.
    reference = await jobs.reference(lease)
    if reference['job_type'] != 'agent.resume':
        raise ValueError('Agent job required.')
    scope = (claim['tenant_id'] + ':' + str(reference['actor_id']) + ':' + str(reference['session_id'])
             if keep_session else str(reference['target_id']) + ':' + str(lease.generation))
    session_id = 'eai-' + hashlib.sha256(scope.encode()).hexdigest()
    client = boto3.client('bedrock-agentcore', region_name=region, config=SDK_CONFIG)
    async def heartbeat():
        while True:
            await asyncio.sleep(4)
            await jobs.heartbeat(lease)
    def invoke():
        response = client.invoke_agent_runtime(agentRuntimeArn=runtime_arn, runtimeSessionId=session_id,
            contentType='application/json', accept='application/json',
            payload=json.dumps({'claim': claim}, separators=(',', ':')).encode())
        stream = response['response']
        try:
            raw = stream.read(8193)
        finally:
            stream.close()
        if len(raw) > 8192:
            raise ValueError('Invalid runtime result.')
        result = json.loads(raw)
        if (result.get('status') != 'ok' or result.get('job_id') != claim['id']
            or result.get('generation') != claim['generation'] or result.get('state') not in {'completed', 'waiting'}):
            raise ValueError('Runtime outcome unresolved.')
        if not keep_session:
            try:
                client.stop_runtime_session(agentRuntimeArn=runtime_arn, runtimeSessionId=session_id)
            except Exception:
                pass  # Idle timeout still reclaims a successfully completed session.
        return result
    pulse = asyncio.create_task(heartbeat())
    work = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        done, _ = await asyncio.wait((pulse, work), return_when=asyncio.FIRST_COMPLETED)
        if pulse in done:
            # Publication completes the job before the response/session stop returns.
            # A terminal heartbeat must not discard that already-committed result.
            try:
                await pulse
            except Exception:
                return await work
            raise ValueError('Worker heartbeat stopped.')
        return await work
    finally:
        pulse.cancel()
        try:
            await pulse
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        # Drain an already-sent request; never send another invocation automatically.
        if not work.done():
            try:
                await asyncio.shield(work)
            except Exception:
                pass
        client.close()
