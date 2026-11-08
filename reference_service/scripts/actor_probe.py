"""Black-box child for the external REF-02 HTTPS verifier; no platform imports."""
import asyncio
from dataclasses import asdict
import json
import secrets
import ssl
import sys
import httpx
from reimbursement_service.composition import database_loop_factory
from reimbursement_service.integrations.platform.client import PlatformClient,PlatformTarget
from reimbursement_service.integrations.platform.proposed import ProposedClients
from reimbursement_service.modules.access.domain import request_hash
from reimbursement_service.modules.access.domain import AccessFailure
from reimbursement_service.modules.access.service import AccessService,Dispatcher,credential_digest


async def run(data):
    context=ssl.create_default_context(cafile=data['certificate'])
    client=PlatformClient(PlatformTarget(data['origin'],data['credential']),
        transport=httpx.AsyncHTTPTransport(verify=context))
    try:
        if data.get('mode')=='canonical':return dict(status=200,local_hash=request_hash(data['payload']))
        if data.get('mode','actor')=='actor':
            service=AccessService(client,Dispatcher('platform-dispatcher',credential_digest(data['ingress']),
                                  frozenset({'trip.read','claim.read','operation.read','claim.submit','claim.information','claim.decide'})))
            headers=[(k.encode(),v.encode()) for k,v in data['headers']]
            actor=await service.authorize(headers,data['payload'],**data['expected'])
            return dict(status=200,scope=actor.scope,replayed=actor.replayed,
                        request_hash=actor.request_hash,local_hash=request_hash(data['payload']))
        proposed=ProposedClients(client,fixture_contract_enabled=data.get('enabled',False))
        result=await getattr(proposed,data['mode'])(**data['arguments'])
        return dict(status=200,result=result)
    except AccessFailure as failure:return dict(status=failure.status,kind=failure.kind.value)
    finally:await client.close()


def main():
    try:
        data=json.loads(sys.stdin.read())
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(run(data))
    except Exception:result=dict(status=599,kind='sanitized_probe_failure')
    print(json.dumps(result))


if __name__=='__main__':main()
