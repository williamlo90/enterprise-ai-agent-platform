"""Public errors expose stable messages, never arbitrary exception details."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from enterprise_ai_platform.api.schemas import ErrorInfo, ErrorResponse

ERRORS = {
    400: ("invalid_request", "Invalid request."),
    401: ("unauthenticated", "Authentication required."),
    403: ("forbidden", "Access denied."),
    404: ("not_found", "Resource not found."),
    405: ("method_not_allowed", "Method not allowed."),
    408: ("request_timeout", "Request body timed out."),
    409: ("conflict", "Request conflicts with current state."),
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
