"""Current authority gates before and after ledger work; proof after commit only."""
import time
from reimbursement_service.modules.access.ports import canonical_payload,request_hash
from reimbursement_service.modules.operations.domain import Identity,Terminal,OperationFailure
from reimbursement_service.modules.operations.ports import OperationsRepository,TerminalProof


class OperationsService:
    def __init__(self,repository:OperationsRepository,proofs:TerminalProof,*,clock=time.time):
        self.repository,self.proofs,self.clock=repository,proofs,clock

    def current(self,actor,identity=None):
        if actor.scope!='actor_only' or self.clock()>=actor.expires_at:raise OperationFailure(401)
        if identity is not None and (actor.actor_id!=identity.actor_id or actor.tenant_id!=identity.tenant_id):raise OperationFailure(404)

    def read_binding(self,actor,payload,operation_id):
        self.current(actor)
        try:canonical_payload(payload)
        except (ValueError,KeyError,TypeError):raise OperationFailure(400) from None
        if (payload['action']!='operation.read' or payload['object']!={'kind':'operation','id':operation_id,'version':1}
                or actor.action!='operation.read' or actor.request_hash!=request_hash(payload)
                or actor.operation_id!=payload['operation_id'] or actor.actor_id!=payload['actor_id']
                or actor.tenant_id!=payload['tenant_id']):raise OperationFailure(403)
        self.current(actor)

    def response(self,terminal,actor):
        try:
            self.current(actor,terminal.identity)
            proof=self.proofs.issue(terminal,authority_deadline=actor.expires_at)
            self.current(actor,terminal.identity)
        except OperationFailure as failure:
            # The ledger transaction has already ended. Failure to authorize or
            # sign its outgoing success leaves the immutable outcome uncertain
            # to this caller; it never rolls back/replays the business effect.
            if failure.status==401:raise OperationFailure(503) from None
            raise
        return dict(operation=terminal.wire(),proof=proof)

    async def commit(self,payload,key,actor,work):
        try:
            canonical_payload(payload)
            identity=Identity(payload['tenant_id'],payload['operation_id'],payload['actor_id'],'platform-dispatcher',
                              payload['action'],key,request_hash(payload),payload['object']['kind'],payload['object']['id'],payload['object']['version'])
        except (ValueError,KeyError,TypeError):raise OperationFailure(400) from None
        self.current(actor,identity)
        if (actor.action!=identity.kind or actor.operation_id!=identity.operation_id
                or actor.request_hash!=identity.request_hash):raise OperationFailure(403)
        terminal=await self.repository.commit(identity,actor,work)
        return self.response(terminal,actor)

    async def replay(self,payload,key,actor):
        """Read-only preflight for the exact authorized original mutation.

        A committed retry must not rerun deadline/pin/eligibility preparation.
        Absence is advisory; commit still acquires the shared identity lock and
        may discover a terminal written while external preparation was running.
        """
        try:
            canonical_payload(payload)
            identity=Identity(payload['tenant_id'],payload['operation_id'],payload['actor_id'],'platform-dispatcher',
                payload['action'],key,request_hash(payload),payload['object']['kind'],payload['object']['id'],payload['object']['version'])
        except (ValueError,KeyError,TypeError):raise OperationFailure(400) from None
        self.current(actor,identity)
        if (actor.action!=identity.kind or actor.operation_id!=identity.operation_id or actor.request_hash!=identity.request_hash):raise OperationFailure(403)
        terminal=await self.repository.existing(identity,actor)
        self.current(actor,identity)
        if terminal is not None and (type(terminal) is not Terminal or terminal.identity!=identity):raise OperationFailure(503)
        return None if terminal is None else self.response(terminal,actor)

    async def lookup(self,operation_id,actor,payload):
        self.read_binding(actor,payload,operation_id)
        terminal=await self.repository.lookup(operation_id,actor)
        return self.response(terminal,actor)

    async def seal(self,identity,actor,payload,*,recovery_authorized=False):
        if recovery_authorized is not True:raise OperationFailure(403)
        self.read_binding(actor,payload,identity.operation_id)
        self.current(actor,identity)
        terminal=await self.repository.seal(identity,actor)
        return self.response(terminal,actor)
