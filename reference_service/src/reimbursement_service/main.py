"""API entry point. Startup verifies readiness, never migrates or provisions."""
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
import uvicorn
from reimbursement_service.api.boundary import RequestBoundary
from reimbursement_service.api.errors import error_response
from reimbursement_service.api.health import router
from reimbursement_service.api.operations import router as operations_router
from reimbursement_service.api.reference import router as reference_router
from reimbursement_service.api.claim_access import router as claim_access_router
from reimbursement_service.api.draft_sources import router as draft_sources_router
from reimbursement_service.api.claims import router as claims_router
from reimbursement_service.api.workspace import router as workspace_router
from reimbursement_service.integrations.platform.claims_authority import ClaimsAuthority
from reimbursement_service.integrations.platform.receipt_pins import ReceiptPinsClient
from reimbursement_service.infrastructure.database.workspace import WorkspaceRepository
from reimbursement_service.composition import resources, database_loop_factory,catalog_resources,claims_service
from reimbursement_service.integrations.security.terminal import Signer
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest
from reimbursement_service.infrastructure.database.connection import finish_cleanup
from reimbursement_service.config import ConfigurationError, load_settings
from reimbursement_service.infrastructure.database.target import TargetError
from reimbursement_service.modules.system.domain import DatabaseUnavailable
from reimbursement_service.modules.operations.domain import OperationFailure
from reimbursement_service.modules.access.domain import AccessFailure
from reimbursement_service.modules.claims.ports import ClaimFailure


def create_app(settings=None, *, resource_factory=resources, operations=None, dispatch=None, recovery=None,
               trips=None,packages=None,trip_dispatch=None,claims=None,claim_dispatch=None):
    settings = settings or load_settings()
    if settings.catalog_enabled and any(value is not None for value in (trips,packages,trip_dispatch)):raise ConfigurationError()
    if any(value is not None for value in (operations,dispatch,recovery)):
        if any(value is None for value in (operations,dispatch,recovery)):raise ConfigurationError()
        if dispatch.dispatcher.credential_digest==recovery.dispatcher.credential_digest:raise ConfigurationError()
    if any(value is not None for value in (trips,packages,trip_dispatch)) and any(value is None for value in (trips,packages,trip_dispatch)):raise ConfigurationError()
    if (claims is None)!=(claim_dispatch is None):raise ConfigurationError()
    if settings.claim_access_enabled and claims is not None:raise ConfigurationError()
    if settings.workflow_enabled and any(x is not None for x in (operations,dispatch,recovery)):raise ConfigurationError()
    database, health = resource_factory(settings)
    catalog=catalog_resources(settings,database) if settings.catalog_enabled else None
    if catalog is not None:trips,packages,trip_dispatch=catalog.trips,catalog.packages,catalog.dispatch
    if settings.claim_access_enabled:
        proofs=Signer(kid=settings.terminal_key_id,private_bytes=bytes.fromhex(settings.terminal_private_hex)) if settings.workflow_enabled else Signer()
        authority=ClaimsAuthority(catalog.eligibility,ReceiptPinsClient(catalog.client)) if settings.workflow_enabled else None
        claims=claims_service(database,proofs,authority=authority)
        claim_dispatch=AccessService(catalog.client,Dispatcher('platform-dispatcher',credential_digest(settings.dispatcher_credential),frozenset({'claim.read'})))
    mutation_dispatch=None
    if settings.workflow_enabled:
        operations=claims.operations
        dispatch=AccessService(catalog.client,Dispatcher('platform-dispatcher',credential_digest(settings.dispatcher_credential),frozenset({'operation.read'})))
        recovery=AccessService(catalog.client,Dispatcher('platform-dispatcher',credential_digest(settings.recovery_credential),frozenset({'operation.read'})))
        mutation_dispatch=AccessService(catalog.client,Dispatcher('platform-dispatcher',credential_digest(settings.dispatcher_credential),frozenset({'claim.submit','claim.information','claim.decide'})))

    @asynccontextmanager
    async def lifespan(app):
        try:
            await database.open()
            yield
        finally:
            async def cleanup():
                try:
                    if catalog is not None:await catalog.client.close()
                finally:await database.close()
            await finish_cleanup(cleanup())

    app = FastAPI(title="Reimbursement service", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.catalog_resources=catalog
    app.state.claims=claims
    app.add_middleware(RequestBoundary)
    app.include_router(router(health))
    if any(value is not None for value in (operations,dispatch,recovery)):
        app.include_router(operations_router(operations,dispatch,recovery))
    if any(value is not None for value in (trips,packages,trip_dispatch)):
        app.include_router(reference_router(trips,packages,trip_dispatch))
    if claims is not None:app.include_router(claim_access_router(claims,claim_dispatch,decision_sources=settings.action_sources_enabled))
    if settings.draft_sources_enabled:app.include_router(draft_sources_router(trips,claims,trip_dispatch,claim_dispatch))
    if mutation_dispatch is not None:app.include_router(claims_router(claims,mutation_dispatch))
    if settings.workflow_enabled:app.include_router(workspace_router(WorkspaceRepository(database),catalog.trips.scopes))

    @app.exception_handler(OperationFailure)
    @app.exception_handler(AccessFailure)
    @app.exception_handler(ClaimFailure)
    async def operation_error(request: Request,exc):
        return error_response(exc.status,request.state.request_id)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        return error_response(exc.status_code, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc):
        return error_response(422, request.state.request_id)

    @app.exception_handler(DatabaseUnavailable)
    async def unavailable(request: Request, exc):
        return error_response(503, request.state.request_id)

    return app


def main():
    try:
        settings = load_settings()
        config = uvicorn.Config(create_app(settings), host=settings.host, port=settings.port,
                                access_log=False, server_header=False)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            runner.run(uvicorn.Server(config).serve())
    except (ConfigurationError, TargetError, DatabaseUnavailable):
        raise SystemExit("Reimbursement service could not start.") from None


if __name__ == "__main__":
    main()
