"""FE-CHAT-01 owner list/create/open/rename on actual local HTTPS/API/owned DB."""
import argparse
import asyncio
import hashlib
import json
import logging
import secrets
import traceback
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
import httpx
from fastapi import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles
from check_knowledge_workspace import Harness as WorkspaceHarness, verify
from frontend_browser_fixture import run_browser
from check_frontend_organization import ROOT, frontend_hashes
from enterprise_ai_platform.authorization_check import require, CheckFailure
from enterprise_ai_platform.composition import ApplicationServices, database_loop_factory
from enterprise_ai_platform.config import Settings, DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.conversation_check import Harness as ConversationHarness
from enterprise_ai_platform.file_check import ORIGIN
from enterprise_ai_platform.knowledge_check import write_json
from enterprise_ai_platform.main import create_app

OUT = ROOT / 'local/fe-chat-01'


class Harness(WorkspaceHarness):
    create_conversation = ConversationHarness.create_conversation
    append = ConversationHarness.append
    history = ConversationHarness.history

    async def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(Settings(env='test', host='127.0.0.1', port=8000), services=ApplicationServices(health=SimpleNamespace(), conversations=self.conversations))), base_url=ORIGIN)

    async def conversations_browser(self):
        try:
            await self._conversations_browser()
        except Exception as error:
            write_json(OUT / 'diagnostic.json', dict(type=type(error).__name__,
                symbol=error.name if isinstance(error, NameError) else None,
                frames=[dict(file=Path(f.filename).name, line=f.lineno, function=f.name) for f in traceback.extract_tb(error.__traceback__)]))
            raise

    async def _conversations_browser(self):
        self.conversations = ConversationService(PostgresConversationRepository(self.database, live_sources=True), ORIGIN)
        created = await self.create_conversation(language='id')
        require(created.status_code == 201, 'fixture-conversation-create')
        cid = created.json()['id']
        question = 'Bagaimana aturan perjalanan? <b>Teks tetap aman</b> ' + 'x' * 3900
        require((await self.append(cid, text=question)).status_code == 202, 'fixture-human-question')
        await self.execute("INSERT INTO {s}.conversations(tenant_id,id,owner_principal_id,language) SELECT %s,gen_random_uuid(),%s,'id' FROM generate_series(1,21)", (self.people[0]['tenant'], self.people[0]['person']))
        colleague = await self.create_conversation(index=1)
        foreign = await self.create_conversation(index=2)
        require(colleague.status_code == 201 and foreign.status_code == 201, 'foreign-owned-fixtures')
        second, second_csrf = await self.session(self.people[0]['tenant'], self.people[0]['person'])
        member_b = await self.member(self.people[2]['tenant'], self.people[0]['person'], grants=(RoleGrant('employee', 'tenant', str(self.people[2]['tenant'])),))
        for index, name in ((0, 'Fiktif Alpha'), (2, 'Fiktif Beta')):
            await self.execute('UPDATE {s}.tenants SET name=%s WHERE id=%s', (name, self.people[index]['tenant']))
        control = secrets.token_urlsafe(32)

        async def invariant(stage):
            values = dict(conversations=await self.scalar('SELECT count(*) FROM {s}.conversations'),
                          owner_conversations=await self.scalar('SELECT count(*) FROM {s}.conversations WHERE owner_principal_id=%s', (self.people[0]['person'],)),
                          messages=await self.scalar('SELECT count(*) FROM {s}.messages'),
                          runs=await self.scalar('SELECT count(*) FROM {s}.runs'))
            write_json(OUT / (stage + '-database-proof.json'), dict(passed=True, invariants=values))
            return values

        def app_for(origin):
            self.conversations.origin = origin
            identity = IdentityService(SimpleNamespace(public_origin=origin), PostgresIdentityRepository(self.database), None, None)
            app = create_app(Settings(env='test', host='127.0.0.1', port=8000).model_copy(update={'browser_context_enabled': True}), services=ApplicationServices(health=SimpleNamespace(), identity=identity, conversations=self.conversations))
            async def page():
                return FileResponse(ROOT / 'frontend/dist/index.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
            for path in ('/', '/chat', '/chat/{conversation_id}', '/access', '/appearance', '/tenants'):
                app.add_api_route(path, page, methods=['GET'], include_in_schema=False)
            async def fixture(request: Request):
                if request.headers.get('x-fixture-control') != control:
                    return JSONResponse({}, status_code=404)
                action = request.headers.get('x-fixture-action')
                if action == 'rename-other':
                    current = await self.conversations.history(second, cid)
                    result = await self.conversations.rename(second, {'origin': origin, 'content-type': 'application/json', 'x-csrf-token': second_csrf}, cid,
                        dict(name='Nama dari sesi lain', expected_version=current['conversation']['version'], key=str(uuid4())))
                    return JSONResponse({'renamed': True, 'version': result['version']})
                if action == 'session':
                    token, _ = await self.session(self.people[0]['tenant'], self.people[0]['person'])
                    response = JSONResponse({'created': True})
                    response.set_cookie('__Host-eai-session', token, secure=True, httponly=True, samesite='lax', path='/')
                    return response
                if action == 'checkpoint':
                    return JSONResponse(await invariant('positive'))
                return JSONResponse({}, status_code=400)
            app.add_api_route('/__fixture/control', fixture, methods=['POST'], include_in_schema=False)
            app.mount('/assets', StaticFiles(directory=str(ROOT / 'frontend/dist/assets')))
            return app

        proof = await run_browser(OUT, 'frontend-conversations-browser.mjs', app_for, dict(owner=self.people[0]['token'], colleague=self.people[1]['token'], foreign=self.people[2]['token'], admin=self.people[3]['token'], conversation=cid, colleague_conversation=colleague.json()['id'], foreign_conversation=foreign.json()['id'], question=question, member_b=str(member_b), control=control), timeout=230)
        values = await invariant('final')
        require(values == dict(conversations=25, owner_conversations=23, messages=1, runs=1), 'create-replay-no-duplicates-or-message-mutation')
        body = await self.scalar('SELECT body FROM {s}.messages WHERE conversation_id=%s', (UUID(cid),))
        require(body == question, 'rename-preserves-original-question')
        write_json(OUT / 'history-invariant.json', dict(passed=True, message_body_sha256=hashlib.sha256(body.encode()).hexdigest(), immutable_language=await self.scalar('SELECT language FROM {s}.conversations WHERE id=%s', (UUID(cid),)), paid_calls=0))
        self.passed('actual_owner_conversations_browser_' + str(len(proof['checks'])) + '_checks')
        self.passed('create_dedup_original_history_and_language_preserved')


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('Explicit --live required for owned test database fixtures.')
    try:
        settings = load_settings(Path('.env'), require_file=True, settings_type=DatabaseProbeSettings)
        base = parse_target(settings.database_url.get_secret_value())
        direct = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
        verify_same_database(base, direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result = runner.run(verify(direct, base, harness_type=Harness, target_version=20, operation='conversations_browser'))
        result['frontend_source_sha256'] = frontend_hashes()
        write_json(OUT / 'database-proof.json', result)
        print(json.dumps({k: v for k, v in result.items() if k not in ('source_sha256', 'frontend_source_sha256')}))
    except Exception as error:
        print('Conversation browser verification failed: ' + (str(error) if isinstance(error, CheckFailure) else type(error).__name__))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
