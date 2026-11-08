"""No raw exceptions or validation input in public diagnostics."""
from starlette.responses import JSONResponse

ERRORS = {
    400: ("invalid_request", "Invalid request.", False),
    401: ("unauthenticated", "Authentication required.", False),
    403: ("forbidden", "Operation denied.", False),
    404: ("not_found", "Resource unavailable.", False),
    405: ("method_not_allowed", "Method unavailable.", False),
    408: ("request_timeout", "Request timed out.", True),
    409: ("conflict", "Operation conflicts with existing state.", False),
    413: ("body_too_large", "Request exceeds limit.", False),
    422: ("invalid_request", "Invalid request.", False),
    500: ("internal_error", "Request failed.", False),
    503: ("dependency_unavailable", "Service unavailable.", True),
}


def error_response(status, request_id):
    code, message, retryable = ERRORS.get(status, ERRORS[500])
    return JSONResponse({"error": dict(code=code, message=message, request_id=request_id,
                                        retryable=retryable)}, status_code=status)
