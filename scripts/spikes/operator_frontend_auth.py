"""William-run auth-slice status/readiness or explicit guarded enable/disable. Lead does not apply."""
import argparse
import asyncio
from contextlib import closing
import hmac
import json
import secrets
import time

import boto3
from psycopg import sql
from check_oidc_runtime import ROOT, safe_transport_logs
from deploy_identity_api import configuration_fingerprint
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import DatabaseProbeSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import RuntimeDatabase, direct_connection, parse_target, verify_same_database
from enterprise_ai_platform.infrastructure.database.migration import migrate

OUT = ROOT / 'local/fe-env-01'


def require(condition):
    if not condition:
        raise ValueError('Auth slice check rejected.')


async def database_ready():
    settings = load_settings(ROOT / '.env', require_file=True, settings_type=DatabaseProbeSettings)
    base = parse_target(settings.database_url.get_secret_value())
    direct = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
    verify_same_database(base, direct)
    require(await migrate(direct) == (15, 15))
    database = RuntimeDatabase(base)
    try:
        await database.open()
        async with database.transaction() as connection:
            row = (await (await connection.execute(sql.SQL('SELECT {}.browser_context_guard(%s,NULL)').format(sql.Identifier(database.schema)), (secrets.token_hex(32),))).fetchone())[0]
            require(row['status'] == 'unauthenticated')
    finally:
        await database.close()
    return settings.database_url.get_secret_value()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'readiness', 'enable', 'disable'))
    parser.add_argument('--apply', action='store_true', help='Only William executes an explicit AWS configuration mutation.')
    args = parser.parse_args()
    safe_transport_logs()
    try:
        plan = json.loads((OUT / 'operator-plan.json').read_text())
        session = boto3.Session(region_name='ap-southeast-1')
        require(session.client('sts').get_caller_identity()['Account'] == plan['account'])
        with closing(session.client('lambda')) as client:
            function = client.get_function_configuration(FunctionName=plan['function_arn'])
            require(function['FunctionArn'] == plan['function_arn'] and function['Role'] == plan['role_arn'])
            require(function['State'] == 'Active' and function['LastUpdateStatus'] == 'Successful')
            variables = function['Environment']['Variables']
            if args.action == 'disable':
                proposed = {**variables, 'APP_AUTH_ENABLED': 'false', 'SPIKE_ENABLED': 'false'}
            else:
                require(function['CodeSha256'] == plan['new_code_sha256'])
                require(variables['PUBLIC_ORIGIN'] == plan['origin'] and variables['OIDC_REDIRECT_URI'] == plan['origin'] + '/api/v1/auth/callback')
                require(variables['APP_FRONTEND_ENABLED'] == variables['APP_BROWSER_CONTEXT_ENABLED'] == 'true')
                proposed = {**variables, 'APP_AUTH_ENABLED': 'true'}
            if args.action in ('readiness', 'enable'):
                with asyncio.Runner(loop_factory=database_loop_factory) as runner:
                    local_url = runner.run(database_ready())
                with closing(session.client('secretsmanager')) as secret_client:
                    private = json.loads(secret_client.get_secret_value(SecretId=variables['SPIKE_SECRET_ARN'])['SecretString'])
                require(hmac.compare_digest(private['database_url'], local_url))
            if args.action == 'enable':
                proof = json.loads((OUT / 'cloud-disabled.json').read_text())
                require(proof['passed'] and 0 <= time.time() - proof['checked_at'] <= 3600)
                require(proof['configuration_fingerprint'] == configuration_fingerprint(function))
                require(variables['APP_AUTH_ENABLED'] == 'false' and variables['SPIKE_ENABLED'] == 'false')
            if args.action in ('enable', 'disable') and args.apply:
                client.update_function_configuration(FunctionName=plan['function_arn'], RevisionId=function['RevisionId'], Environment={'Variables': proposed})
        result = {'passed': True, 'action': args.action, 'applied': args.apply and args.action in ('enable', 'disable'),
                  'auth_enabled_before': variables.get('APP_AUTH_ENABLED') == 'true', 'actual_user_login': 'not_tested'}
        if args.action == 'readiness':
            result.update(schema_revision=15, restricted_browser_context_capability=True, cloud_secret_matches_local_runtime=True)
            (OUT / 'runtime-readiness.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'passed': False, 'action': args.action, 'category': type(error).__name__, 'actual_user_login': 'not_tested'}))
        print('Stop and diagnose with Lead; do not broaden IAM or rerun an uncertain mutation.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
