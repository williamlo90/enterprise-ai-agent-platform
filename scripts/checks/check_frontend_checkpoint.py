"""FE-CHECK-01: one combined local knowledge journey; no AWS or paid inference.

Only disposable namespaces/roles in the existing test database are changed.
Sessions, object storage, malware verdict, extraction and embeddings are fixtures;
React, HTTPS API, permissions, jobs and PostgreSQL transactions are actual.
"""
import argparse
import asyncio
import json
import logging
import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

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
from enterprise_ai_platform.infrastructure.database.organization import PostgresOrganizationRepository
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.modules.files.service import FilesService
from enterprise_ai_platform.modules.authorization.domain import RoleGrant
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.knowledge_check import write_json
from check_knowledge_audience import Harness as AudienceHarness
from enterprise_ai_platform.conversation_check import ControlledSources
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.integrations.checkpoints.rag_graph import build_rag_graph
from enterprise_ai_platform.file_check import ORIGIN

OUT = ROOT / 'local/fe-check-01'


class CreatorHarness(AudienceHarness):
    async def creator_revocation(self):
        self.sources = ControlledSources()
        self.conv_repo = PostgresConversationRepository(self.database, live_sources=True)
        self.conversations = ConversationService(self.conv_repo, ORIGIN, sources=self.sources)
        first = await self.document('Travel claims must be submitted within seven days. Fictional creator policy.')
        require((await self.mutate(first, 'publish')).status_code == 200, 'creator-publishes-original')
        current = await self.new_version(first, 'Travel claims must be submitted within ten days. Fictional creator replacement.')
        require((await self.mutate(current, 'publish')).status_code == 200, 'creator-publishes-replacement')
        creator = self.people[0]
        # Owned organization maintenance fixture: revoke only KM, retain employee.
        await self.execute("DELETE FROM {s}.membership_roles WHERE tenant_id=%s AND membership_id=%s AND role='knowledge_manager'", (creator['tenant'], creator['member']))
        creator['token'], creator['csrf'] = await self.session(creator['tenant'], creator['person'])
        require(await self.scalar("SELECT count(*) FROM {s}.membership_roles WHERE tenant_id=%s AND membership_id=%s AND role='knowledge_manager'", (creator['tenant'], creator['member'])) == 0, 'creator-really-has-no-km')
        require((await self.source_http(current, index=0)).status_code == 200, 'creator-explicit-grant-still-readable-after-km-loss')
        self.passed('creator_without_km_can_read_only_through_remaining_explicit_grant')

        # The real conversation/graph/history pipeline belongs to the creator.
        # Its model factory is deterministic; no commercial provider is called.
        self.people[4] = dict(creator)
        worker, conversation, models = await self.turn()
        await worker.run(build_rag_graph)
        before = await self.history(conversation, index=4)
        require(before.status_code == 200, 'creator-history-before')
        model = [m for m in before.json()['messages'] if m['author_kind'] == 'model'][-1]
        require(model['text'] and not model['redacted'] and model['sources'], 'creator-visible-real-history-before-revoke')

        selection, _ = await self.select(current, index=1, retain_existing=False)
        require(selection.status_code == 201 and selection.json()['selected_count'] == 0, 'other-km-selects-empty-acl')
        response, _ = await self.apply(current, 'specific_people', selection.json()['selection_id'], index=1)
        require(response.status_code == 200, 'other-km-revokes-creator-explicit-grant')
        require(await self.scalar('SELECT count(*) FROM {s}.document_acl WHERE document_id=%s AND principal_id=%s', (UUID(current['document_id']), creator['person'])) == 0, 'creator-grant-actually-removed')
        require((await self.source_http(first, index=0)).status_code == 404 and (await self.source_http(current, index=0)).status_code == 404, 'creator-cannot-read-either-version')
        require((await self.search_http('claims', index=0)).json()['items'] == [], 'creator-search-no-source-after-revoke')
        after = await self.history(conversation, index=4)
        require(after.status_code == 200, 'creator-still-owns-conversation')
        redacted = [m for m in after.json()['messages'] if m['author_kind'] == 'model'][-1]
        require(redacted['redacted'] and redacted['text'] is None and redacted['citations'] == [] and redacted['sources'] == [] and redacted['feedback'] is None, 'creator-real-history-whole-redaction')
        self.passed('creator_without_km_and_explicit_grant_loses_current_superseded_search_and_history_content')
        require((await self.source_http(first, index=1)).status_code == 200 and (await self.source_http(current, index=1)).status_code == 200, 'independent-km-authority-retained')
        self.passed('other_knowledge_manager_retains_independent_authority_after_creator_revoke')
        self.passed('deterministic_model_graph_only_no_paid_calls')


class Harness(WorkspaceHarness):
    async def checkpoint_browser(self):
        control = secrets.token_urlsafe(32)
        tenant = self.people[0]['tenant']
        other_tenant = self.people[2]['tenant']
        member_b = await self.member(other_tenant, self.people[0]['person'], grants=(
            RoleGrant('knowledge_manager', 'tenant', str(other_tenant)),))
        for index, name in ((0, 'Anton Wijaya'), (3, 'Administrator Uji'), (4, 'Budi Santoso')):
            await self.execute('UPDATE {s}.principals SET display_name=%s WHERE id=%s',
                               (name, self.people[index]['person']))
        for tid, name in ((tenant, 'Fiktif Alpha'), (other_tenant, 'Fiktif Beta')):
            await self.execute('UPDATE {s}.tenants SET name=%s WHERE id=%s', (name, tid))
        scope = uuid4()
        await self.execute("INSERT INTO {s}.authorization_scopes(tenant_id,scope_kind,scope_ref) VALUES(%s,'knowledge_collection',%s)", (tenant, scope))
        scoped_person = uuid4()
        await self.member(tenant, scoped_person, grants=(RoleGrant('knowledge_manager', 'knowledge_collection', str(scope)),))
        scoped_token, _ = await self.session(tenant, scoped_person)

        async def invariant(stage):
            values = dict(
                documents=await self.scalar('SELECT count(*) FROM {s}.documents'),
                versions=await self.scalar('SELECT count(*) FROM {s}.document_versions'),
                withdrawn=await self.scalar('SELECT count(*) FROM {s}.documents WHERE withdrawn_at IS NOT NULL'),
                superseded=await self.scalar("SELECT count(*) FROM {s}.document_versions WHERE publication_state='superseded'"),
                withdrawn_versions=await self.scalar("SELECT count(*) FROM {s}.document_versions WHERE publication_state='withdrawn'"),
            )
            write_json(OUT / (stage + '-database-proof.json'), dict(passed=True, stage=stage, invariants=values))
            return values

        def app_for(origin):
            self.knowledge.origin = origin
            identity = IdentityService(SimpleNamespace(public_origin=origin), PostgresIdentityRepository(self.database), None, None)
            organization = OrganizationService(PostgresOrganizationRepository(self.database), origin)
            files = FilesService(self.file_repo, self.store, origin)
            app = create_app(Settings(env='test', host='127.0.0.1', port=8000).model_copy(update={'browser_context_enabled': True}), services=ApplicationServices(
                health=SimpleNamespace(), identity=identity, organization=organization, knowledge=self.knowledge, files=files))

            async def page():
                return FileResponse(ROOT / 'frontend/dist/index.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

            for path in ('/', '/organization', '/organization/members/{member}', '/knowledge', '/knowledge/upload', '/knowledge/documents/{document}', '/search', '/sources/{version}', '/chat', '/access', '/appearance', '/tenants'):
                app.add_api_route(path, page, methods=['GET'], include_in_schema=False)

            async def fixture(request: Request):
                if request.headers.get('x-fixture-control') != control:
                    return JSONResponse({}, status_code=404)
                action = request.headers.get('x-fixture-action')
                if action == 'process':
                    claim = await self.scheduler.claim()
                    if claim.get('job_type') == 'knowledge.ingest':
                        await self.process_knowledge(claim)
                    elif claim.get('job_type') == 'file.inspect':
                        await self.inspector.process(claim)
                    else:
                        return JSONResponse({}, status_code=409)
                    return JSONResponse({'processed': True})
                if action == 'session':
                    # Controlled identity boundary, not a Cognito login assertion.
                    token, _ = await self.session(tenant, self.people[0]['person'])
                    response = JSONResponse({'created': True})
                    response.set_cookie('__Host-eai-session', token, secure=True, httponly=True, samesite='lax', path='/')
                    return response
                if action == 'new-employee':
                    person = uuid4()
                    await self.member(tenant, person, grants=(RoleGrant('employee', 'tenant', str(tenant)),))
                    token, _ = await self.session(tenant, person)
                    # Delivered over the private fixture channel, never written to evidence.
                    return JSONResponse({'token': token})
                if action == 'checkpoint':
                    return JSONResponse(await invariant('journey-positive'))
                if action == 'replacement-checkpoint':
                    values = await invariant('journey-replacement')
                    require(values == dict(documents=1, versions=2, withdrawn=0, superseded=1, withdrawn_versions=0), 'replacement-state-before-withdraw')
                    return JSONResponse(values)
                return JSONResponse({}, status_code=400)

            app.add_api_route('/__fixture/control', fixture, methods=['POST'], include_in_schema=False)
            app.mount('/assets', StaticFiles(directory=str(ROOT / 'frontend/dist/assets')))
            return app

        proof = await run_browser(OUT, 'frontend-checkpoint-browser.mjs', app_for, dict(
            manager=self.people[0]['token'], employee=self.people[4]['token'], admin=self.people[3]['token'],
            foreign=self.people[2]['token'], scoped=scoped_token, control=control,
            membership=str(self.people[4]['member']), member_a=str(self.people[0]['member']), member_b=str(member_b)), timeout=200)
        values = await invariant('journey-final')
        require(values == dict(documents=1, versions=2, withdrawn=1, superseded=0, withdrawn_versions=2), 'one-document-two-versions-no-duplicate-commit')
        require(await self.scalar("SELECT department='Finance' AND job_title='Manager' FROM {s}.organization_profiles WHERE membership_id=%s", (self.people[4]['member'],)) is True, 'organization-write-persists')
        self.passed('combined_knowledge_browser_' + str(len(proof['checks'])) + '_checks')
        self.passed('organization_and_document_lifecycle_database_invariants')


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--creator-only', action='store_true', help='Targeted FE-22 gap, without rerunning browser journey.')
    args = parser.parse_args()
    if not args.live:
        parser.error('Explicit --live required for owned test database fixtures.')
    try:
        settings = load_settings(Path('.env'), require_file=True, settings_type=DatabaseProbeSettings)
        base = parse_target(settings.database_url.get_secret_value())
        direct = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
        verify_same_database(base, direct)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            result = runner.run(verify(direct, base, harness_type=CreatorHarness if args.creator_only else Harness, target_version=20, operation='creator_revocation' if args.creator_only else 'checkpoint_browser'))
        result['frontend_source_sha256'] = frontend_hashes()
        write_json(OUT / ('creator-proof.json' if args.creator_only else 'database-proof.json'), result)
        print(json.dumps({k: v for k, v in result.items() if k not in ('source_sha256', 'frontend_source_sha256')}))
    except Exception as error:
        print('Knowledge checkpoint verification failed: ' + (str(error) if isinstance(error, CheckFailure) else type(error).__name__))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
