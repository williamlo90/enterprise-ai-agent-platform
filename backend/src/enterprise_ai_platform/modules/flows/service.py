"""One bounded lease attempt. No blind mutation retry after a send is uncertain."""
from enterprise_ai_platform.modules.flows.domain import FlowFailure,VerifiedTerminal
from enterprise_ai_platform.modules.flows.ports import FlowRepository,FlowTransport,FlowProofs


class FlowWorker:
    def __init__(self,repository:FlowRepository,transport:FlowTransport,proofs:FlowProofs,jobs,credential_hash):
        self.repository,self.transport,self.proofs,self.jobs,self.credential_hash=repository,transport,proofs,jobs,credential_hash

    async def call(self,operation,lease,**values):
        return await self.repository.call(operation,dict(credential_hash=self.credential_hash,id=str(lease.id),generation=lease.generation,**values))

    async def process(self,lease):
        try:packet=await self.call('prepare',lease)
        except FlowFailure as error:
            if error.code not in {'forbidden','held'}:raise
            try:await self.jobs.fail(lease,'forbidden')
            except Exception:pass
            return dict(state='held')
        try:
            if packet['mode']=='dispatch':response=await self.transport.execute(packet)
            elif packet['mode']=='recover':
                response=await self.transport.lookup(packet)
                # A 404 alone is not evidence. The separate recovery endpoint
                # serializes an absent tombstone against any late old request.
                if response is None:
                    await self.jobs.heartbeat(lease)
                    # Obtain renewed original context after extending the lease.
                    packet=await self.call('prepare',lease)
                    response=await self.transport.seal(packet)
            else:raise FlowFailure('invalid')
            terminal=self.proofs.verify(response,packet['identity'])
            if type(terminal) is not VerifiedTerminal:raise FlowFailure('invalid')
            if terminal.state=='rejected':
                # Immutable rejected is held; never relabel it a seal or loop.
                return await self.call('hold',lease)
            await self.jobs.heartbeat(lease)
            pins=await self.call('pins',lease)
            envelope,receipts=self.proofs.settlement(terminal,packet['identity'],pins['batch'])
            return await self.call('settle',lease,settlement=envelope,receipt_settlement=receipts)
        except Exception:
            # A worker crash/time-out/proof failure may follow a remote commit.
            # Persist recovery state only while this worker still owns the lease.
            try:return await self.call('uncertain',lease)
            except Exception:raise FlowFailure('stale') from None
