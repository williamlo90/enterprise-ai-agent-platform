"""Internal coordination only: no network, business retries, issuance or public API."""
import hashlib
import re
from uuid import UUID
from enterprise_ai_platform.modules.jobs.domain import JobRequest, Lease, ERROR_CODES, identifier
from enterprise_ai_platform.modules.jobs.ports import JobRepository, JobFailure


class JobService:
    def __init__(self, repository: JobRepository, credential: str):
        if not isinstance(credential, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', credential):
            raise ValueError('Invalid job credential.')
        self.repository = repository
        self._credential_hash = hashlib.sha256(('eai:job-caller:v1:' + credential).encode()).hexdigest()

    async def _call(self, operation, payload):
        result = await self.repository.call(operation, {**payload, 'credential_hash': self._credential_hash})
        if not isinstance(result, dict) or result.get('status') not in {'ok', 'empty', 'yield'}:
            status = result.get('status') if isinstance(result, dict) else None
            raise JobFailure(status if status in {'stale','held','invalid','forbidden','unauthenticated','capacity','conflict','missing'} else 'unavailable')
        return result

    async def enqueue(self, request: JobRequest):
        if not isinstance(request, JobRequest):
            raise ValueError('A job request is required.')
        return await self._call('enqueue', request.payload())

    async def claim(self):
        return await self._call('claim', {})

    async def inspect(self, job_id: UUID):
        identifier(job_id)
        return await self._call('inspect', {'id': str(job_id)})

    async def _leased(self, operation, lease, extra=None):
        if not isinstance(lease, Lease):
            raise ValueError('A lease handle is required.')
        # Tenant is deliberately not passed as a caller assertion. The registry binds it.
        return await self._call(operation, {'id': str(lease.id), 'generation': lease.generation, **(extra or {})})

    async def reference(self, lease: Lease):
        return await self._leased('reference', lease)

    async def heartbeat(self, lease: Lease):
        return await self._leased('heartbeat', lease)

    async def complete(self, lease: Lease):
        return await self._leased('complete', lease)

    async def fail(self, lease: Lease, error_code: str):
        if error_code not in ERROR_CODES:
            raise ValueError('Unknown failure category.')
        return await self._leased('fail', lease, {'error_code': error_code})
