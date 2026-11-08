"""POL-01 real configured factories/lifespans with owned DB namespaces and TLS CA."""
import argparse
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
import httpx
from check_policy_publication import Harness as PublicationHarness,Target,DRAFTS,ORIGIN
from check_trip_catalog import Harness as TripHarness
from check_knowledge_workspace import verify
from enterprise_ai_platform import composition as platform_composition
from enterprise_ai_platform.config import Settings,DatabaseProbeSettings,load_settings
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.authorization_check import require,CheckFailure
from enterprise_ai_platform.infrastructure.database.connection import parse_target,verify_same_database
from enterprise_ai_platform.internal_api import create_internal_app
from enterprise_ai_platform.main import create_app
from enterprise_ai_platform.integrations.reimbursement.policies import PolicyReceiver
from enterprise_ai_platform.integrations.reimbursement.trips import TripCatalogReader
from enterprise_ai_platform.knowledge_check import write_json
from reimbursement_service import composition as reference_composition
from reimbursement_service.config import Settings as ReferenceSettings
from reimbursement_service.main import create_app as reference_app
from reimbursement_service.integrations.platform.client import PlatformClient


def database_url(database):
    # Private value used only in Settings; never persisted or printed.
    values=database.pool.kwargs
    return 'postgresql://'+quote(values['user'],safe='')+':'+quote(values['password'],safe='')+'@'+values['host']+':'+str(values['port'])+'/'+quote(values['dbname'],safe='')+'?sslmode=verify-full'


class Harness(TripHarness):
    def __init__(self,*args):
        super().__init__(*args);self.configured_apps=[];self.configured_reference_database=None
        self.configured_started=time.monotonic()

    def passed(self,name):
        super().passed(name)
        # Fixed source-authored check labels only; no sessions/URLs/DB secrets.
        write_json(Path('local/pol01/configured-progress.json'),dict(
            phase=getattr(self,'configured_phase','setup'),completed_checks=len(self.checks),
            last_check=name,elapsed_seconds=round(time.monotonic()-self.configured_started,1)))

    def owned_database_factory(self,target,**kwargs):
        require(target.user=='eai_runtime_'+self.schema.rsplit('_',1)[1],'configured-owned-runtime-target')
        # The production factory otherwise uses its fixed platform namespace.
        # Inject only this explicitly owned test namespace, retaining actual pool,
        # TLS driver, role validation, repositories and every business service.
        return self.real_database(target,schema=self.schema,**kwargs)

    def internal_application(self,delegation):
        self.configured_phase='internal_startup'
        settings=Settings(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url=database_url(self.database),
            internal_auth_enabled=True,reimbursement_enabled=False,auth_enabled=False,files_enabled=False,knowledge_enabled=False,
            conversations_enabled=False,organization_enabled=False,browser_context_enabled=False)
        self.real_database=platform_composition.RuntimeDatabase
        with patch.object(platform_composition,'RuntimeDatabase',side_effect=self.owned_database_factory):app=create_internal_app(settings)
        require(app.state.services.delegation is not None,'configured-internal-delegation')
        self.configured_apps.append(app);return app

    def reference_application(self,database,platform_client,dispatcher,trust):
        self.configured_phase='reference_startup'
        settings=ReferenceSettings(database_url=database_url(database),schema=database.schema,catalog_enabled=True,
            platform_origin=platform_client.target.origin,platform_credential=platform_client.target.credential,dispatcher_credential=dispatcher)
        def resources(value):
            # Call the actual service resource factory, capturing its owned pool.
            actual=reference_composition.resources(value);self.configured_reference_database=actual[0];return actual
        def client(target):return PlatformClient(target,transport=httpx.AsyncHTTPTransport(verify=trust))
        with patch.object(reference_composition,'PlatformClient',side_effect=client):app=reference_app(settings,resource_factory=resources)
        require(app.state.catalog_resources is not None,'configured-reference-catalog-services')
        self.configured_reference_app=app;return app

    def public_application(self,receiver,trip_reader,trust):
        self.configured_phase='public_startup'
        issuer='https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_Test'
        domain='https://fixture.auth.ap-southeast-1.amazoncognito.com'
        private=receiver.signer.private_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
        settings=Settings(env='test',host='127.0.0.1',port=8000,database_enabled=True,database_url=database_url(self.database),
            auth_enabled=True,auth_callback_logging_controlled=True,auth_jwks_background_refresh=False,auth_verifier_key=Fernet.generate_key().decode(),
            oidc_issuer=issuer,oidc_client_id='synthetic-client',oidc_client_secret='private-fixture',public_origin=ORIGIN,
            oidc_redirect_uri=ORIGIN+'/api/v1/auth/callback',oidc_authorization_endpoint=domain+'/oauth2/authorize',oidc_token_endpoint=domain+'/oauth2/token',
            oidc_jwks_uri=issuer+'/.well-known/jwks.json',internal_auth_enabled=True,browser_context_enabled=True,knowledge_enabled=True,
            reimbursement_enabled=True,reimbursement_origin=receiver.target.origin,reimbursement_dispatcher_credential=receiver.target.dispatcher_credential,
            reimbursement_signing_private_key=private,reimbursement_signing_kid=receiver.signer.kid,reimbursement_settlement_key_id=receiver.target.settlement_key_id,
            files_enabled=False,organization_enabled=False,conversations_enabled=False)
        def policy(target,signer):return PolicyReceiver(target,signer,transport=httpx.AsyncHTTPTransport(verify=trust))
        def trips(target,signer):return TripCatalogReader(target,signer,transport=httpx.AsyncHTTPTransport(verify=trust))
        # External fixtures supply only namespace and trusted loopback CA. No
        # application services, signatures, authorizations, SQL or DTOs are mocked.
        with patch.object(platform_composition,'RuntimeDatabase',side_effect=self.owned_database_factory),patch.object(platform_composition,'PolicyReceiver',side_effect=policy),patch.object(platform_composition,'TripCatalogReader',side_effect=trips):app=create_app(settings)
        require(app.state.services.policies is app.state.resources.policies and app.state.services.trips is app.state.resources.trips,'configured-platform-service-wiring')
        require(app.state.services.delegation.receipts is None,'unbound-human-receipts-still-fail-closed')
        self.configured_apps.append(app);return app

    async def publication_cases(self,client,database,schema):
        require(all(app.state.resources.readiness.ready for app in self.configured_apps),'configured-platform-lifespans-entered')
        self.configured_phase='trip_cases'
        await TripHarness.publication_cases(self,client,database,schema)
        self.configured_phase='publication_cases'
        await PublicationHarness.publication_cases(self,client,database,schema)

    async def policy_publication(self):
        try:await super().policy_publication()
        except Exception as error:
            code='';current=error
            while current is not None:
                state=getattr(current,'sqlstate',None)
                if isinstance(state,str) and len(state)==5 and state.isalnum():code='-'+state;break
                current=current.__context__
            detail=str(error) if isinstance(error,CheckFailure) else type(error).__name__
            last=self.checks[-1] if self.checks else 'none'
            raise CheckFailure('configured-'+getattr(self,'configured_phase','setup')+'-'+detail+code+'-after-'+last) from None
        require(len(self.configured_apps)==2,'configured-two-platform-apps')
        for app in self.configured_apps:
            require(not app.state.resources.readiness.ready and app.state.resources.database.pool.closed,'configured-platform-lifecycle-drained')
            require(all(c.http.is_closed for c in app.state.resources.reimbursement_clients),'configured-outbound-clients-closed')
        require(self.configured_reference_app.state.catalog_resources.client.http.is_closed and self.configured_reference_database.pool.closed,'configured-reference-lifecycle-drained')
        self.passed('actual_configured_startup_factories_and_lifespans_run_both_apps_without_injected_business_services_and_drain_all_owned_resources')


def main():
    logging.disable(logging.CRITICAL)
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--output',type=Path,default=Path('local/pol01/configured-runtime-proof.json'));args=parser.parse_args()
    if not args.live:parser.error('Explicit --live required for owned disposable database proof.')
    try:
        paths=DRAFTS+[Path('scripts/checks')/p for p in ['check_policy_draft.py','check_policy_publication.py','check_trip_catalog.py','check_reference_actor.py','check_policy_configured_runtime.py']]+list(Path('reference_service/src').rglob('*.py'))+list(Path('reference_service/src').rglob('*.sql'))
        snapshots={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        settings=load_settings(args.env_file,require_file=True,settings_type=DatabaseProbeSettings)
        base=parse_target(settings.database_url.get_secret_value());direct=parse_target(settings.database_direct_url.get_secret_value(),direct=True);verify_same_database(base,direct)
        Harness.reference_admin=Target(direct.host,direct.port,direct.dbname,direct.user,direct.password,direct.channel_binding)
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:result=runner.run(verify(direct,base,harness_type=Harness,target_version=21,operation='policy_publication',direct_timeout=240))
        require(all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in snapshots.items()),'configured-runtime-source-stable')
        result.update(scope='configured_startup_policy_publication_and_trip_component_not_whole_POL_acceptance',draft_sha256=snapshots,
            external_fixtures='owned platform DB namespace + loopback trusted CA; fictional sessions/trips/documents; OIDC login and cloud not exercised',
            actual_configured_factories=True,business_services_injected=False,service_revision=3,transport='public ASGI/browser guard; actual interservice certificate-verified TLS',database_isolation='one existing physical PostgreSQL; separate owned schemas and restricted logins')
        args.output.parent.mkdir(parents=True,exist_ok=True);write_json(args.output,result);print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','draft_sha256')}))
    except Exception as error:
        print('Configured policy runtime proof failed: '+(str(error) if isinstance(error,CheckFailure) else 'sanitized_failure'));raise SystemExit(1) from None


if __name__=='__main__':main()
