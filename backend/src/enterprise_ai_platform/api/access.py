"""Shared HTTP entry to authorization; feature routes supply trusted resource references."""

from contextlib import asynccontextmanager
from enterprise_ai_platform.api.identity import cookie, browser_headers, SESSION_COOKIE
from enterprise_ai_platform.modules.authorization.ports import AccessFailure


@asynccontextmanager
async def authorized_resource(request, ref, action, *, changed_fields=frozenset()):
    request.state.authorization_required = True
    service = request.app.state.services.authorization
    if service is None:
        raise AccessFailure(503)
    # Roles, actor IDs, tenant headers, bearer tokens and prompt fields are not authority.
    async with service.access(cookie(request, SESSION_COOKIE), ref, action, changed_fields=changed_fields,
                              headers=browser_headers(request)) as resource:
        yield resource
