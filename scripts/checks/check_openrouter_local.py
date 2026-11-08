"""Opt-in, two-call synthetic LangGraph/RagTurn smoke; no DB or AWS clients."""
import argparse
import asyncio
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/src'))

from dotenv import dotenv_values
from langgraph.checkpoint.memory import InMemorySaver
from enterprise_ai_platform.composition import build_openrouter_test_adapters
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.integrations.models.openrouter import FREE_MODEL
from enterprise_ai_platform.modules.models.domain import EmbeddingResult, Usage, ModelFailure
from enterprise_ai_platform.modules.rag.domain import sha
from enterprise_ai_platform.modules.rag.service import RagTurn

SOURCES = [
    'backend/src/enterprise_ai_platform/integrations/models/openrouter.py',
    'backend/src/enterprise_ai_platform/composition.py',
    'backend/src/enterprise_ai_platform/modules/rag/service.py',
    'backend/src/enterprise_ai_platform/modules/rag/domain.py',
    'backend/src/enterprise_ai_platform/integrations/checkpoints/rag_graph.py',
    'scripts/checks/check_openrouter_local.py',
]


class FixtureEmbedding:
    """Fixed vector for plumbing ONLY; no semantic retrieval quality claim."""
    last_usage = None

    def __init__(self, before):
        self.before = before

    def embed(self, texts, *, input_type):
        self.before('embedding', 512)
        self.last_usage = Usage(1, 0)
        return EmbeddingResult(((1.0,) + (0.0,) * 1023,), self.last_usage)


class FixtureSaver(InMemorySaver):
    def __init__(self):
        super().__init__()
        self.binding = {}

    def config(self):
        return {'configurable': {'thread_id': 'synthetic-' + str(id(self))}}


class FixtureRepository:
    """Synthetic authority/retrieval/settlement, not production DB/ACL evidence."""
    def __init__(self, question, language, report, save):
        self.question, self.language, self.report, self.save = question, language, report, save
        self.events, self.settlements = [], []

    async def runtime(self, operation, payload):
        self.events.append(operation)
        return {'deadline': time.time() + 50}

    async def call(self, operation, payload):
        if operation == 'input':
            return {'question': self.question, 'language': self.language}
        if operation == 'reserve':
            kind = payload['kind']
            self.report['reservations'].append({'kind': kind, 'provider': 'OpenRouter' if kind == 'chat' else 'fixture'})
            self.save()
            return {'authority_expires_at': time.time() + 40, 'settlement_receipt': sha(kind)}
        if operation == 'search':
            text = 'Karyawan tetap mendapat 12 hari cuti tahunan.'
            return {'items': [dict(version_id='00000000-0000-0000-0000-000000000001', version_no=1,
                index_generation=1, access_revision=1, chunk_no=0, page=1, start=0, text=text, text_digest=sha(text))]}
        if operation == 'publish':
            return payload
        if operation in ('recheck', 'hold'):
            return {}
        raise RuntimeError('Unexpected fixture operation')

    async def settle(self, kind, receipt, usage):
        self.settlements.append({'kind': kind, 'usage': asdict(usage) if usage is not None else None})


async def run():
    logging.disable(logging.CRITICAL)
    # Do not mutate .env or global application configuration; select only this key.
    key = (dotenv_values(ROOT / '.env').get('OPENROUTER_API_KEY') or '').strip()
    folder = ROOT / 'local/openrouter-integration' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    proof = folder / 'proof.json'
    report = {'passed': False, 'status': 'started', 'model': FREE_MODEL,
        'scope': 'preliminary synthetic LangGraph/RagTurn integration; fixture retrieval/authority/embedding; not DB, UI, Nova Pro or M1 acceptance',
        'max_live_requests': 2, 'automatic_retry': False, 'paid_fallback': False,
        'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES},
        'reservations': [], 'cases': []}
    def save():
        temporary = proof.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        temporary.replace(proof)
    save()
    try:
        for number, (question, language, expected) in enumerate([
            ('Berapa hak cuti tahunan karyawan tetap?', 'id', 'answer'),
            ('Does this policy reimburse dental implants?', 'en', 'insufficient_evidence'),
        ]):
            if number:
                await asyncio.sleep(3.2)
            repo = FixtureRepository(question, language, report, save)
            turn = RagTurn(repo, None, FixtureSaver(), SimpleNamespace(), None)
            observer = turn.observer(asyncio.get_running_loop())
            with build_openrouter_test_adapters(observer, api_key=key, embedding=FixtureEmbedding(observer),
                    fictional_only=True, max_requests=1) as models:
                turn.model_factory = lambda before: nullcontext(models)
                result = await turn.run(build_rag_graph)
                case = {'case': number + 1, 'expected': expected, 'actual': result['kind'],
                        'passed': result['kind'] == expected, 'http_attempts': models.chat.attempts,
                        'last_cost_usd': models.chat.last_cost_usd, 'settlements': repo.settlements,
                        'http_status': models.chat.last_status_code, 'provider_failure': models.chat.last_failure,
                        'citations': len(result['citations']), 'graph_events': repo.events}
                report['cases'].append(case)
                save()
                if not case['passed']:
                    raise ModelFailure('invalid_output')
        report.update(passed=True, status='passed')
    except Exception as error:
        report.update(status='failed', failure=error.code if isinstance(error, ModelFailure) else 'sanitized_integration_failure')
    save()
    print(json.dumps({'passed': report['passed'], 'status': report['status'], 'cases': report['cases'],
                      'failure': report.get('failure'), 'proof': str(proof)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--fictional-only', action='store_true')
    args = parser.parse_args()
    if not args.live or not args.fictional_only:
        parser.error('Both --live and --fictional-only are required; nothing has been sent.')
    raise SystemExit(asyncio.run(run()))
