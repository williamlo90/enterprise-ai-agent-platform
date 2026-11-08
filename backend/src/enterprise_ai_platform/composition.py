"""Composition root; resources are owned by one application lifecycle."""

from contextlib import asynccontextmanager,contextmanager,AsyncExitStack
from enterprise_ai_platform.modules.workspace.ports import WorkspacePort
from enterprise_ai_platform.modules.workspace.service import WorkspaceService
from enterprise_ai_platform.infrastructure.database.workspace import PostgresWorkspaceRepository
from enterprise_ai_platform.integrations.reimbursement.workspace import WorkspaceReader
from enterprise_ai_platform.modules.administration.ports import AdministrationPort
from enterprise_ai_platform.modules.administration.service import AdministrationService
from enterprise_ai_platform.infrastructure.database.administration import PostgresAdministrationRepository
from enterprise_ai_platform.modules.privacy.registry import PrivacyRegistryService
from enterprise_ai_platform.infrastructure.database.privacy_registry import PostgresPrivacyPolicyRepository
from dataclasses import dataclass
import asyncio
import sys
from contextlib import suppress

from enterprise_ai_platform.modules.conversations.ports import ConversationPort
from enterprise_ai_platform.modules.conversations.service import ConversationService
from enterprise_ai_platform.infrastructure.database.conversations import PostgresConversationRepository
from enterprise_ai_platform.modules.knowledge.ports import KnowledgePort
from enterprise_ai_platform.modules.knowledge.service import KnowledgeService
from enterprise_ai_platform.infrastructure.database.knowledge import PostgresKnowledgeRepository
from enterprise_ai_platform.modules.organization.ports import OrganizationPort
from enterprise_ai_platform.modules.organization.service import OrganizationService
from enterprise_ai_platform.infrastructure.database.organization import PostgresOrganizationRepository
from enterprise_ai_platform.modules.files.ports import FilesServicePort
from enterprise_ai_platform.modules.files.service import FilesService
from enterprise_ai_platform.infrastructure.database.files import PostgresFileRepository
from enterprise_ai_platform.infrastructure.storage.s3 import S3Files,S3_CONFIG
import boto3

from enterprise_ai_platform.config import ConfigurationError, Settings
from enterprise_ai_platform.modules.delegation.ports import DelegationServicePort
from enterprise_ai_platform.modules.delegation.service import DelegationService
from enterprise_ai_platform.infrastructure.database.delegation import PostgresDelegationRepository
from enterprise_ai_platform.integrations.security.delegation import RSADelegationVerifier
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, parse_target
from enterprise_ai_platform.infrastructure.runtime import RuntimeReadiness
from enterprise_ai_platform.modules.system.ports import HealthServicePort
from enterprise_ai_platform.modules.system.service import HealthService
from enterprise_ai_platform.modules.identity.domain import CognitoProvider, OidcRejected
from enterprise_ai_platform.modules.identity.ports import IdentityServicePort
from enterprise_ai_platform.modules.authorization.ports import AuthorizationServicePort
from enterprise_ai_platform.modules.authorization.service import AuthorizationService
from enterprise_ai_platform.infrastructure.database.authorization import PostgresAuthorizationRepository
from enterprise_ai_platform.modules.identity.service import IdentityService
from enterprise_ai_platform.infrastructure.database.identity import PostgresIdentityRepository
from enterprise_ai_platform.integrations.identity.authentication import CognitoAuthentication, FernetVerifierProtector
from enterprise_ai_platform.integrations.models.nova import NovaChat,CohereEmbedding,NOVA_SDK_CONFIG
from enterprise_ai_platform.integrations.models.bedrock import ENDPOINT
from enterprise_ai_platform.modules.models.ports import ModelAdapters
from enterprise_ai_platform.modules.policies.ports import PolicyPort
from enterprise_ai_platform.modules.trips.ports import TripPort
from enterprise_ai_platform.modules.trips.service import TripsService
from enterprise_ai_platform.modules.policies.service import PolicyService
from enterprise_ai_platform.infrastructure.database.policies import PostgresPolicyRepository
from enterprise_ai_platform.infrastructure.database.trips import PostgresTripRepository
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyReceiver,PolicyTarget
from enterprise_ai_platform.integrations.reimbursement.trips import TripCatalogReader
from enterprise_ai_platform.integrations.security.catalog import RSACatalogSigner
from enterprise_ai_platform.infrastructure.database.connection import finish_cleanup
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import UnsupportedAlgorithm
import os
from enterprise_ai_platform.modules.receipts.ports import ReceiptServicePort
from enterprise_ai_platform.modules.receipts.service import ReceiptPinService
from enterprise_ai_platform.infrastructure.database.receipts import PostgresReceiptRepository
from enterprise_ai_platform.integrations.security.receipt_configuration import receipt_security
from enterprise_ai_platform.integrations.security.receipt_settlement import ReceiptSettlement
from enterprise_ai_platform.integrations.security.delegation import RSADelegationSigner
from enterprise_ai_platform.integrations.reimbursement.receipt_access import ClaimReceiptReader
from enterprise_ai_platform.integrations.reimbursement.receipt_download import ClaimDownloadAuthority
from enterprise_ai_platform.modules.actions.ports import ActionPort
from enterprise_ai_platform.modules.actions.service import ActionService
from enterprise_ai_platform.infrastructure.database.actions import PostgresActionRepository,PostgresHumanReceiptReader
from enterprise_ai_platform.integrations.reimbursement.decision_source import DecisionSourceReader
from enterprise_ai_platform.modules.drafts.ports import DraftServicePort
from enterprise_ai_platform.modules.drafts.service import DraftService
from enterprise_ai_platform.infrastructure.database.drafts import PostgresDraftRepository
from enterprise_ai_platform.integrations.reimbursement.draft_sources import DraftSourceReader
from enterprise_ai_platform.infrastructure.database.flows import PostgresLockedClaimBindings,PostgresFlowRepository
from enterprise_ai_platform.modules.flows.service import FlowWorker
from enterprise_ai_platform.integrations.reimbursement.workflow import WorkflowTransport
from enterprise_ai_platform.integrations.security.flow_terminal import FlowTerminalVerifier
from enterprise_ai_platform.modules.jobs.service import JobService
from enterprise_ai_platform.infrastructure.database.jobs import PostgresJobRepository


@dataclass(frozen=True)
class ApplicationServices:
    health: HealthServicePort
    conversations: ConversationPort | None = None
    knowledge: KnowledgePort | None = None
    organization: OrganizationPort | None = None
    files: FilesServicePort | None = None
    identity: IdentityServicePort | None = None
    authorization: AuthorizationServicePort | None = None
    delegation: DelegationServicePort | None = None
    policies: PolicyPort | None = None
    trips: TripPort | None = None
    receipts: ReceiptServicePort | None = None
    drafts: DraftServicePort | None = None
    actions: ActionPort | None = None
    workspace: WorkspacePort | None = None
    administration: AdministrationPort | None = None
    privacy_policy: PrivacyRegistryService | None = None


@dataclass(frozen=True)
class ApplicationResources:
    readiness: RuntimeReadiness
    database: RuntimeDatabase | None = None
    identity: IdentityServicePort | None = None
    authorization: AuthorizationServicePort | None = None
    delegation: DelegationServicePort | None = None
    policies: PolicyPort | None = None
    trips: TripPort | None = None
    receipts: ReceiptServicePort | None = None
    drafts: DraftServicePort | None = None
    actions: ActionPort | None = None
    workspace: WorkspacePort | None = None
    administration: AdministrationPort | None = None
    privacy_policy: PrivacyRegistryService | None = None
    oidc: CognitoAuthentication | None = None
    conversations: ConversationPort | None = None
    knowledge: KnowledgePort | None = None
    organization: OrganizationPort | None = None
    files: FilesServicePort | None = None
    storage: S3Files | None = None
    background_refresh: bool = True
    reimbursement_clients: tuple = ()

    @asynccontextmanager
    async def lifecycle(self):
        refresh_task = None
        try:
            if self.database is not None:
                await self.database.open()
            if self.oidc is not None and self.background_refresh:
                async def refresh_keys():
                    while True:
                        with suppress(Exception):
                            await self.oidc.refresh()
                        await asyncio.sleep(30)
                refresh_task = asyncio.create_task(refresh_keys())
            self.readiness.ready = True
            yield
        finally:
            self.readiness.ready = False
            async def cleanup():
                async with AsyncExitStack() as stack:
                    if self.database is not None:stack.push_async_callback(self.database.close)
                    if self.storage is not None:stack.push_async_callback(self.storage.close)
                    for client in self.reimbursement_clients:stack.push_async_callback(client.close)
                    if refresh_task is not None:
                        refresh_task.cancel()
                        with suppress(asyncio.CancelledError):await refresh_task
            await finish_cleanup(cleanup())


def build_resources(settings: Settings | None = None) -> ApplicationResources:
    target=signer=None
    receipt_keys=None
    if settings is not None and settings.receipts_enabled:
        try:receipt_keys=receipt_security(settings.receipt_security.get_secret_value())
        except (ValueError,TypeError):raise ConfigurationError('Invalid private receipt security configuration.') from None
    if settings is not None and settings.reimbursement_enabled:
        # Parse/validate secrets before allocating any provider/database clients.
        try:
            target=PolicyTarget(settings.reimbursement_origin,settings.reimbursement_dispatcher_credential.get_secret_value(),settings.reimbursement_settlement_key_id)
            private=serialization.load_pem_private_key(settings.reimbursement_signing_private_key.get_secret_value().encode(),password=None)
            signer=RSACatalogSigner(private,settings.reimbursement_signing_kid,settings.delegation_issuer,'reference-reimbursement','platform-dispatcher')
        except (ValueError,TypeError,UnsupportedAlgorithm):raise ConfigurationError('Invalid private reimbursement integration configuration.') from None
    database = None
    if settings is not None and settings.database_enabled:
        if settings.database_url is None:
            raise ConfigurationError("Missing or invalid configuration: DATABASE_URL")
        database = RuntimeDatabase(parse_target(settings.database_url.get_secret_value()), max_size=settings.database_pool_max_size)
        import os
        if os.environ.get('EAI_JOURNAL_ENABLED','false') not in ('true','false'):raise ValueError('Invalid journal flag.')
        if os.environ.get('EAI_JOURNAL_ENABLED')=='true':
            from enterprise_ai_platform.integrations.security.recovery_journal import RecoveryJournal
            database.security_journal=RecoveryJournal(os.environ['EAI_JOURNAL_WRITER_ARN'],os.environ['EAI_JOURNAL_ID'],'platform')
    delegation = None
    if settings is not None and settings.internal_auth_enabled:
        delegation = DelegationService(PostgresDelegationRepository(database),RSADelegationVerifier(),settings.delegation_issuer)
    identity = oidc = authorization = None
    if settings is not None and settings.auth_enabled:
        try:
            provider = CognitoProvider(settings.oidc_issuer, settings.oidc_client_id,
                settings.oidc_client_secret.get_secret_value(), settings.oidc_redirect_uri, settings.public_origin,
                settings.oidc_authorization_endpoint, settings.oidc_token_endpoint, settings.oidc_jwks_uri)
            repository = PostgresIdentityRepository(database)
            authorization = AuthorizationService(PostgresAuthorizationRepository(database), provider.public_origin)
            oidc = CognitoAuthentication(provider, repository)
            identity = IdentityService(provider, repository, oidc,
                FernetVerifierProtector(settings.auth_verifier_key.get_secret_value()))
        except (OidcRejected, ValueError, TypeError):
            raise ConfigurationError("Invalid authentication configuration.") from None
    files = storage = None
    if settings is not None and settings.files_enabled:
        storage = S3Files(boto3.Session(region_name='ap-southeast-1').client('s3', config=S3_CONFIG, endpoint_url='https://s3.ap-southeast-1.amazonaws.com'), settings.files_bucket)
        files = FilesService(PostgresFileRepository(database),storage,settings.public_origin)
    knowledge=KnowledgeService(PostgresKnowledgeRepository(database),settings.public_origin) if settings is not None and settings.knowledge_enabled else None
    conversations=ConversationService(PostgresConversationRepository(database,live_sources=True),settings.public_origin) if settings is not None and settings.conversations_enabled else None
    organization=OrganizationService(PostgresOrganizationRepository(database),settings.public_origin) if settings is not None and settings.organization_enabled else None
    administration=AdministrationService(PostgresAdministrationRepository(database),settings.public_origin) if settings is not None and settings.administration_enabled else None
    privacy_policy=PrivacyRegistryService(PostgresPrivacyPolicyRepository(database),settings.public_origin) if settings is not None and settings.privacy_enabled else None
    policies=trips=receipts=drafts=actions=workspace=None;reimbursement_clients=()
    if target is not None:
        receiver=PolicyReceiver(target,signer);reader=TripCatalogReader(target,signer)
        reimbursement_clients=(receiver,reader)
        policies=PolicyService(PostgresPolicyRepository(database),settings.public_origin,delegation,receiver=receiver)
        if settings.flow_enabled:policies.locked=PostgresLockedClaimBindings(database)
        trips=TripsService(PostgresTripRepository(database),reader)
        if receipt_keys is not None:
            key,credential_hash,proofs=receipt_keys
            receipts=ReceiptPinService(PostgresReceiptRepository(database),delegation,proofs=proofs,settlement=ReceiptSettlement(key))
            v1=RSADelegationSigner(private,settings.reimbursement_signing_kid,settings.delegation_issuer,'reference-reimbursement','platform-dispatcher')
            claim_reader=ClaimReceiptReader(target,v1);reimbursement_clients=(*reimbursement_clients,claim_reader)
            files.claim_authority=ClaimDownloadAuthority(claim_reader,key,credential_hash)
            if settings.drafts_enabled:
                draft_reader=DraftSourceReader(target,v1,key,credential_hash);reimbursement_clients=(*reimbursement_clients,draft_reader)
                drafts=DraftService(PostgresDraftRepository(database),settings.public_origin,draft_reader)
                if settings.actions_enabled:
                    decision_reader=DecisionSourceReader(target,v1,key,credential_hash);reimbursement_clients=(*reimbursement_clients,decision_reader)
                    actions=ActionService(PostgresActionRepository(database),settings.public_origin,draft_reader,decision_reader)
                    delegation.receipts=PostgresHumanReceiptReader(database)
                    if settings.flow_enabled:
                        workspace_reader=WorkspaceReader(target,signer);reimbursement_clients=(*reimbursement_clients,workspace_reader)
                        workspace=WorkspaceService(PostgresWorkspaceRepository(database),workspace_reader,draft_reader)
                        if conversations is not None:
                            from enterprise_ai_platform.integrations.reimbursement.provenance import ServiceProvenance
                            conversations.repository.service_sources=ServiceProvenance(workspace_reader,files.claim_authority)
    return ApplicationResources(privacy_policy=privacy_policy,administration=administration,workspace=workspace,actions=actions,drafts=drafts,receipts=receipts,policies=policies,trips=trips,reimbursement_clients=reimbursement_clients,organization=organization,conversations=conversations,knowledge=knowledge,files=files, storage=storage, readiness=RuntimeReadiness(database=database), database=database, identity=identity, authorization=authorization, delegation=delegation, oidc=oidc,
        background_refresh=True if settings is None else settings.auth_jwks_background_refresh)


def database_loop_factory():
    # psycopg async does not support the default Windows Proactor loop.
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def workflow_worker(settings,database,credential,*,transport=None):
    """Explicit worker factory; API startup never starts a worker or provisions."""
    if not settings.flow_enabled:raise ConfigurationError('Workflow worker is disabled.')
    try:
        private=serialization.load_pem_private_key(settings.reimbursement_signing_private_key.get_secret_value().encode(),password=None)
        signer=RSADelegationSigner(private,settings.reimbursement_signing_kid,settings.delegation_issuer,'reference-reimbursement','platform-dispatcher')
        target=PolicyTarget(settings.reimbursement_origin,settings.reimbursement_dispatcher_credential.get_secret_value())
        key,caller_hash,proofs=receipt_security(settings.receipt_security.get_secret_value())
        jobs=JobService(PostgresJobRepository(database),credential)
        channel=WorkflowTransport(target,signer,settings.reimbursement_recovery_credential.get_secret_value(),transport=transport)
        return FlowWorker(PostgresFlowRepository(database),channel,FlowTerminalVerifier(proofs.registry,key,caller_hash),jobs,jobs._credential_hash)
    except Exception:raise ConfigurationError('Invalid workflow worker configuration.') from None


def build_services(resources: ApplicationResources) -> ApplicationServices:
    return ApplicationServices(privacy_policy=resources.privacy_policy,administration=resources.administration,workspace=resources.workspace,actions=resources.actions,drafts=resources.drafts,receipts=resources.receipts,trips=resources.trips,policies=resources.policies,organization=resources.organization,conversations=resources.conversations,knowledge=resources.knowledge,files=resources.files, health=HealthService(readiness=resources.readiness), identity=resources.identity, authorization=resources.authorization, delegation=resources.delegation)


def receipt_service(database,delegation=None,*,proofs=None,settlement=None,clock=None):
    """Explicit shared-database private composition; no resource allocation."""
    kwargs={} if clock is None else {'clock':clock}
    return ReceiptPinService(PostgresReceiptRepository(database,**kwargs),delegation,proofs=proofs,settlement=settlement,**kwargs)


@contextmanager
def build_model_adapters(before_call,*,fictional_apac=False,session=None):
    """Explicit run-scoped factory; never enabled by API startup or tenant input.

    Singapore-only product use remains rejected. Caller owns authorization,
    fictional-data scope and the durable paid-attempt observer.
    """
    if fictional_apac is not True or not callable(before_call) or os.environ.get('AWS_BEARER_TOKEN_BEDROCK'):
        raise ConfigurationError('Model integration requires an explicit fictional APAC scope and budget observer.')
    session=session or boto3.Session(region_name='ap-southeast-1')
    client=session.client('bedrock-runtime',region_name='ap-southeast-1',endpoint_url=ENDPOINT,config=NOVA_SDK_CONFIG)
    try:yield ModelAdapters(NovaChat(client,before_call),CohereEmbedding(client,before_call))
    finally:client.close()


@contextmanager
def build_openrouter_test_adapters(before_call, *, api_key, embedding, fictional_only=False,
                                   max_requests=6, transport=None):
    """Explicit local test composition; never selected by API/tenant configuration.

    Caller supplies a controlled embedding port; this factory creates no AWS
    clients. Free-provider data retention is unsuitable for enterprise documents.
    """
    if fictional_only is not True or not callable(getattr(embedding, 'embed', None)):
        raise ConfigurationError('OpenRouter testing requires fictional data and an explicit embedding fixture.')
    import httpx
    from enterprise_ai_platform.integrations.models.openrouter import OpenRouterFreeChat
    with httpx.Client(timeout=httpx.Timeout(20, connect=3), trust_env=False,
                      follow_redirects=False, transport=transport) as client:
        yield ModelAdapters(OpenRouterFreeChat(client, api_key, before_call, max_requests=max_requests), embedding)


async def build_rag_turn(database,jobs,claim,*,model_factory,mode='vector',service_sources=None,prompt_version='rag-extractive-v1'):
    """Explicit worker composition; model factory owns fictional/residency/budget policy.

    API startup never launches this graph or provisions an executor/model.
    """
    from uuid import UUID
    import asyncio
    from enterprise_ai_platform.modules.jobs.ports import Lease
    from enterprise_ai_platform.modules.rag.service import RagTurn
    from enterprise_ai_platform.modules.conversations.service import DenySources
    from enterprise_ai_platform.infrastructure.database.checkpoints import PostgresCheckpointRepository
    from enterprise_ai_platform.infrastructure.database.rag import PostgresRagRepository
    from enterprise_ai_platform.integrations.checkpoints.langgraph import BoundCheckpointSaver
    from enterprise_ai_platform.modules.rag.domain import PROMPT_VERSION,CANDIDATE_PROMPT_VERSION
    if prompt_version not in (PROMPT_VERSION,CANDIDATE_PROMPT_VERSION):raise ConfigurationError('Unknown RAG prompt version.')
    lease=Lease(UUID(claim['id']),UUID(claim['tenant_id']),claim['generation'])
    reference=await jobs.reference(lease)
    if reference['job_type']!='agent.resume':raise ConfigurationError('RAG requires an agent lease.')
    binding=dict(credential_hash=jobs._credential_hash,tenant_id=str(lease.tenant_id),run_id=reference['target_id'],job_id=str(lease.id),generation=lease.generation,epoch=0)
    lock=asyncio.Lock()
    repository=PostgresRagRepository(database,binding,lock=lock)
    checkpoints=PostgresCheckpointRepository(database,DenySources(),lock=lock,service_sources=service_sources)
    checkpoints.source_factory=repository.source_access
    saver=BoundCheckpointSaver(checkpoints,**binding)
    started=await saver.start(model_ref='apac.amazon.nova-pro-v1:0',prompt_version=prompt_version,config_version=1)
    repository.binding['epoch']=started['epoch']
    await repository.runtime('begin',{})
    return RagTurn(repository,model_factory,saver,jobs,lease,mode=mode,prompt_version=prompt_version)


async def build_assisted_turn(database,jobs,claim,*,source,model_factory,service_sources=None):
    """Explicit worker admission; no models or workers start with the API."""
    from uuid import UUID
    import asyncio
    from enterprise_ai_platform.modules.jobs.ports import Lease
    from enterprise_ai_platform.modules.assisted.service import AssistedTurn
    from enterprise_ai_platform.modules.conversations.domain import SourceRef
    from enterprise_ai_platform.modules.conversations.service import DenySources
    from enterprise_ai_platform.infrastructure.database.checkpoints import PostgresCheckpointRepository
    from enterprise_ai_platform.infrastructure.database.assisted import PostgresAssistedRepository
    from enterprise_ai_platform.infrastructure.database.rag import PostgresRagRepository
    from enterprise_ai_platform.integrations.checkpoints.langgraph import BoundCheckpointSaver
    lease=Lease(UUID(claim['id']),UUID(claim['tenant_id']),claim['generation'])
    ref=await jobs.reference(lease)
    if ref['job_type']!='agent.resume':raise ConfigurationError('Assisted graph requires an agent lease.')
    binding=dict(credential_hash=jobs._credential_hash,tenant_id=str(lease.tenant_id),run_id=ref['target_id'],job_id=str(lease.id),generation=lease.generation,epoch=0)
    lock=asyncio.Lock();repository=PostgresAssistedRepository(database,binding,lock=lock)
    checkpoints=PostgresCheckpointRepository(database,DenySources(),lock=lock,source_factory=repository.source_access,service_sources=service_sources)
    saver=BoundCheckpointSaver(checkpoints,**binding,sources=(SourceRef('platform','assisted_request',ref['target_id'],'1','owner','1'),))
    started=await saver.start(model_ref='apac.amazon.nova-pro-v1:0',prompt_version='workflow-native-tools-v1',config_version=1)
    repository.binding['epoch']=started['epoch'];budget=PostgresRagRepository(database,repository.binding,lock=lock)
    data=await repository.call('input',{})
    if data['proposal'] is None:await budget.runtime('begin',{})
    return AssistedTurn(repository,saver,source,model_factory,jobs,lease,budget)


@contextmanager
def build_workflow_model(before_call,*,fictional_apac=False,session=None):
    """Explicit native planner factory with existing model residency restrictions."""
    if fictional_apac is not True or not callable(before_call) or os.environ.get('AWS_BEARER_TOKEN_BEDROCK'):
        raise ConfigurationError('Workflow model requires explicit fictional APAC scope and budget observer.')
    from enterprise_ai_platform.integrations.models.workflow import NovaWorkflowPlanner
    session=session or boto3.Session(region_name='ap-southeast-1')
    client=session.client('bedrock-runtime',region_name='ap-southeast-1',endpoint_url=ENDPOINT,config=NOVA_SDK_CONFIG)
    try:yield NovaWorkflowPlanner(client,before_call)
    finally:client.close()
