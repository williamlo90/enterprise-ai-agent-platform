"""Public errors expose stable messages, never arbitrary exception details."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from enterprise_ai_platform.api.schemas import ErrorInfo, ErrorResponse
from enterprise_ai_platform.modules.authorization.ports import AccessFailure
from enterprise_ai_platform.modules.conversations.ports import ConversationFailure
from enterprise_ai_platform.modules.knowledge.ports import KnowledgeFailure
from enterprise_ai_platform.modules.organization.ports import OrganizationFailure
from enterprise_ai_platform.modules.files.ports import FileFailure
from enterprise_ai_platform.modules.identity.ports import BrowserContextFailure
from enterprise_ai_platform.modules.policies.ports import PolicyFailure
from enterprise_ai_platform.modules.trips.ports import TripFailure
from enterprise_ai_platform.modules.receipts.ports import ReceiptFailure
from enterprise_ai_platform.modules.drafts.ports import DraftFailure
from enterprise_ai_platform.modules.actions.ports import ActionFailure
from enterprise_ai_platform.modules.privacy.ports import PrivacyDownloadExpired

ERRORS = {
    400: ("invalid_request", "Invalid request."),
    401: ("unauthenticated", "Authentication required."),
    403: ("forbidden", "Access denied."),
    404: ("not_found", "Resource not found."),
    405: ("method_not_allowed", "Method not allowed."),
    408: ("request_timeout", "Request body timed out."),
    409: ("conflict", "Request conflicts with current state."),
    410: ("expired", "Private download expired."),
    413: ("body_too_large", "Request body exceeds the allowed size."),
    415: ("unsupported_media_type", "Unsupported media type."),
    422: ("validation_error", "Request validation failed."),
    429: ("rate_limited", "Request limit exceeded."),
    500: ("internal_error", "Internal server error."),
    503: ("service_unavailable", "Service temporarily unavailable."),
}


def error_response(status: int, request_id: str) -> JSONResponse:
    code, message = ERRORS.get(status, ("request_failed", "Request failed."))
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(error=ErrorInfo(
            code=code, message=message, request_id=request_id,
            retryable=status in {408, 429, 503},
        )).model_dump(),
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PrivacyDownloadExpired)
    async def privacy_download_expired(request: Request, exc: PrivacyDownloadExpired) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)

    @app.exception_handler(ActionFailure)
    async def action_error(request: Request, exc: ActionFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)
    @app.exception_handler(DraftFailure)
    async def draft_error(request: Request, exc: DraftFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)
    @app.exception_handler(ReceiptFailure)
    async def receipt_error(request: Request, exc: ReceiptFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)
    @app.exception_handler(TripFailure)
    async def trip_error(request: Request, exc: TripFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)
    @app.exception_handler(PolicyFailure)
    async def policy_error(request: Request, exc: PolicyFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)
    @app.exception_handler(OrganizationFailure)
    async def organization_error(request: Request, exc: OrganizationFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)
    @app.exception_handler(BrowserContextFailure)
    async def browser_context_error(request: Request, exc: BrowserContextFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)
    @app.exception_handler(ConversationFailure)
    async def conversation_error(request: Request, exc: ConversationFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)

    @app.exception_handler(KnowledgeFailure)
    async def knowledge_error(request: Request, exc: KnowledgeFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)

    @app.exception_handler(FileFailure)
    async def file_error(request: Request, exc: FileFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)

    @app.exception_handler(AccessFailure)
    async def access_error(request: Request, exc: AccessFailure) -> JSONResponse:
        return error_response(exc.status, request.state.request_id)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        response = error_response(exc.status_code, request.state.request_id)
        # Preserve only the protocol-required Allow header, with fixed method tokens.
        allow = (exc.headers or {}).get("Allow", "")
        if exc.status_code == 405 and allow and all(
            token.strip() in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
            for token in allow.split(",")
        ):
            response.headers["Allow"] = allow
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        status = 400 if any(item["type"] == "json_invalid" for item in exc.errors()) else 422
        return error_response(status, request.state.request_id)
