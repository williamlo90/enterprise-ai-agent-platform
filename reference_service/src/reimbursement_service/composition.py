"""The sole service composition root; resources belong to each app instance."""
import asyncio
import sys
import time
from dataclasses import dataclass
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase
from reimbursement_service.infrastructure.database.target import parse_target
from reimbursement_service.modules.system.service import HealthService
from reimbursement_service.infrastructure.database.reference import PostgresTrips,PostgresPackages
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.integrations.platform.catalog import CatalogAuthorityAdapter
from reimbursement_service.integrations.platform.policies import PolicyEligibilityClient
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest
from reimbursement_service.modules.trips.service import TripsService
from reimbursement_service.modules.policies.service import PackagesService


def resources(settings):
    database = RuntimeDatabase(parse_target(settings.database_url), settings.schema, max_size=settings.pool_size)
    import os
    if os.environ.get('EAI_JOURNAL_ENABLED','false') not in ('true','false'):raise ValueError('Invalid journal flag.')
    if os.environ.get('EAI_JOURNAL_ENABLED')=='true':
        from reimbursement_service.integrations.security.recovery_journal import RecoveryJournal
        database.security_journal=RecoveryJournal(os.environ['EAI_JOURNAL_WRITER_ARN'],os.environ['EAI_JOURNAL_ID'],'reimbursement')
    return database, HealthService(database)


@dataclass(frozen=True)
class CatalogResources:
    client:PlatformClient
    trips:TripsService
    packages:PackagesService
    dispatch:AccessService
    eligibility:PolicyEligibilityClient


def catalog_resources(settings,database,*,platform_transport=None):
    target=PlatformTarget(settings.platform_origin,settings.platform_credential)
    client=PlatformClient(target) if platform_transport is None else PlatformClient(target,transport=platform_transport)
    authority=CatalogAuthorityAdapter(client,credential_digest(settings.dispatcher_credential))
    dispatch=AccessService(client,Dispatcher('platform-dispatcher',credential_digest(settings.dispatcher_credential),frozenset({'trip.read'})))
    return CatalogResources(client,TripsService(PostgresTrips(database),authority),PackagesService(PostgresPackages(database),authority),dispatch,PolicyEligibilityClient(client))


def database_loop_factory():
    # Psycopg async connections cannot use Windows' default Proactor loop.
    return asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()


def claims_service(database,proofs,*,authority=None,clock=time.time):
    """Explicit foundation factory; does not mount a writer or open resources.

    The same database owns reads and the REF-03 mutation/ledger UoW. Production
    consent/policy/pin providers remain unbound until their owning integration
    tasks are complete; omitted authority always denies new effects.
    """
    from reimbursement_service.infrastructure.database.claims import PostgresClaims
    from reimbursement_service.infrastructure.database.operations import PostgresOperations
    from reimbursement_service.modules.operations.service import OperationsService
    from reimbursement_service.modules.claims.service import ClaimsService
    return ClaimsService(PostgresClaims(database,clock=clock),OperationsService(PostgresOperations(database,clock=clock),proofs,clock=clock),
        authority=authority,clock=clock)
