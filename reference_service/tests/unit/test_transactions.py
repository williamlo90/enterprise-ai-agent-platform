import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4
import pytest
from reimbursement_service.infrastructure.database.connection import RuntimeDatabase, finish_cleanup
from reimbursement_service.infrastructure.database.target import parse_target
from reimbursement_service.modules.system.domain import ActorContext, DatabaseUnavailable


class Cursor:
    async def fetchone(self): return (True,True)


class Connection:
    def __init__(self):
        self.pgconn=SimpleNamespace(ssl_in_use=True)
        self.events=[]; self.closed=False
    @asynccontextmanager
    async def transaction(self):
        self.events.append('begin')
        try: yield
        except BaseException: self.events.append('rollback'); raise
        else: self.events.append('commit')
    async def execute(self, statement, params=None):
        self.events.append(params)
        return Cursor()
    async def close(self): self.closed=True


class Pool:
    def __init__(self,**kwargs): self.connection=Connection(); self.returned=0; self.closed=False
    async def open(self): pass
    async def close(self,**kwargs): self.closed=True
    async def getconn(self,**kwargs): return self.connection
    async def putconn(self,conn): self.returned+=1


def database():
    return RuntimeDatabase(parse_target('postgresql://reimbursement_runtime:fictional@db.example.test/service'),pool_factory=Pool)


def test_context_is_cleared_on_every_checkout_and_exception_rolls_back():
    async def run():
        db=database(); context=ActorContext(uuid4(),uuid4())
        async with db.transaction(context): pass
        with pytest.raises(ValueError):
            async with db.transaction(): raise ValueError('domain failure')
        params=[x for x in db.pool.connection.events if isinstance(x,tuple)]
        assert params[0][-2:]==(str(context.tenant_id),str(context.actor_id))
        assert params[1][-2:]==('','')
        assert db.pool.connection.events.count('commit')==1
        assert db.pool.connection.events.count('rollback')==1 and db.pool.returned==2
    asyncio.run(run())


def test_cancel_discards_connection_and_returns_capacity():
    async def run():
        db=database(); entered=asyncio.Event()
        async def work():
            async with db.transaction(): entered.set(); await asyncio.Event().wait()
        task=asyncio.create_task(work()); await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert db.pool.connection.closed and db.pool.returned==1
    asyncio.run(run())


def test_cleanup_survives_repeated_cancellation():
    async def run():
        started=asyncio.Event(); done=asyncio.Event(); finished=[]
        async def cleanup(): started.set(); await done.wait(); finished.append(True)
        task=asyncio.create_task(finish_cleanup(cleanup()))
        await started.wait(); task.cancel(); await asyncio.sleep(0); task.cancel()
        done.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert finished==[True]
    asyncio.run(run())
