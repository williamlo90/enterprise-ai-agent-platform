"""Private Gateway Lambda target. No confirmation, approval, dispatch, or payment tools."""
import asyncio
import json
import os

from enterprise_ai_platform.agentcore_execution import bound_job
from enterprise_ai_platform.composition import build_resources, database_loop_factory
from enterprise_ai_platform.config import load_settings
from enterprise_ai_platform.infrastructure.database.rag import PostgresRagRepository
from enterprise_ai_platform.infrastructure.database.assisted import PostgresAssistedRepository
from enterprise_ai_platform.integrations.agentcore.clients import load_environment, phase
from enterprise_ai_platform.modules.assisted.domain import content


async def execute(event, context):
    if type(event) is not dict or set(event) != {'binding', 'arguments'} or len(json.dumps(event)) > 131072:
        raise ValueError('Invalid tool request.')
    if type(event['binding']) is not dict or type(event['arguments']) is not dict:
        raise ValueError('Invalid tool objects.')
    metadata = context.client_context.custom
    gateway_id = os.environ['EAI_AGENTCORE_GATEWAY_ID']
    name = metadata.get('bedrockAgentCoreToolName', '')
    if metadata.get('bedrockAgentCoreGatewayId') != gateway_id or not name.startswith('platform___'):
        raise ValueError('Untrusted gateway.')
    tool = name.removeprefix('platform___')
    permitted = {'search_policy': {'search'}, 'get_policy_version': {'recheck'},
                 'create_reimbursement_draft': {'create', 'replace', 'preview'}}
    binding = dict(event['binding'])
    epoch = binding.pop('epoch')
    arguments = dict(event['arguments'])
    operation = arguments.pop('operation')
    fields = {'search': {'query', 'vector', 'mode'}, 'recheck': {'items'},
              'create': set(), 'replace': {'content'}, 'preview': set()}
    if (tool not in permitted or operation not in permitted[tool] or set(arguments) != fields[operation]
        or type(epoch) is not int or not 1 <= epoch < 10**18):
        raise ValueError('Tool operation not permitted.')
    resources = build_resources(load_settings(None))
    async with resources.lifecycle():
        claim, jobs, lease, ref, route, authoritative = await bound_job(resources, binding)
        authoritative['epoch'] = epoch
        if (tool == 'create_reimbursement_draft') != (route['agent_kind'] == 'assisted'):
            raise ValueError('Tool does not match the admitted job.')
        repository = (PostgresAssistedRepository if route['agent_kind'] == 'assisted' else PostgresRagRepository)(resources.database, authoritative)
        # The RPC checks current lease, epoch, original session, membership, and source access.
        current = await repository.call('input', {})
        if operation == 'search':
            question = current['question']
            if current.get('prior_question'):
                question = current['prior_question'] + ' ' + question
            if arguments['query'] != question:
                raise ValueError('Search is not bound to the current question.')
        if route['agent_kind'] == 'assisted':
            selected = current['selection']
            facts = await resources.drafts.source.read(current['context'], selected['trip_id'], selected['claim_id'])
            if operation in {'create', 'preview'}:
                arguments['source'] = facts.wire()
            if operation == 'replace':
                prepared = content(authoritative['run_id'], current['proposal'], selected, facts.wire()['material']['trip'])
                if arguments['content'] != prepared:
                    raise ValueError('Draft differs from the admitted proposal.')
                arguments['content'] = prepared
        with phase('tool.' + tool):
            return await repository.call(operation, arguments)


def handler(event, context):
    try:
        load_environment(os.environ['EAI_AGENTCORE_SECRET_ARN'], os.environ.get('AWS_REGION', 'ap-southeast-1'))
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            return runner.run(execute(event, context))
    except Exception as error:
        # Lambda platform/telemetry must not receive raw SQL, credentials, or private text.
        print(json.dumps({'event': 'tool_held', 'error_type': type(error).__name__, 'status': getattr(error, 'status', None)}), flush=True)
        return {'status': 'held'}
