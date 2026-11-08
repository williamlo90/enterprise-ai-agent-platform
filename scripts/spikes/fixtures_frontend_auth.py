"""William-run fictional Alpha/Beta memberships for one explicitly selected existing principal."""
import argparse
import asyncio
import json
import hashlib
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
from check_oidc_runtime import ROOT
from enterprise_ai_platform.composition import database_loop_factory
from enterprise_ai_platform.config import MigrationSettings, load_settings
from enterprise_ai_platform.infrastructure.database.connection import direct_connection, parse_target
from enterprise_ai_platform.infrastructure.database.migration import migrate

OUT = ROOT / 'local/fe-env-01'
RECORD = OUT / 'fixtures.json'


def require(condition):
    if not condition:
        raise ValueError('Auth slice check rejected.')


def database_operator(database, session_user):
    # Authenticated DB bootstrap channel, not an invented person or the role recipient.
    issuer = 'urn:eai:bootstrap:database-operator'
    subject = database + ':' + session_user
    return {'id': str(uuid5(NAMESPACE_URL, issuer + ':' + subject)), 'issuer': issuer, 'subject': subject,
            'channel': 'authenticated_database_bootstrap', 'human_oidc_identity_verified': False}


def target_fingerprint(target):
    return hashlib.sha256(json.dumps([target.host, target.port, target.dbname, target.user], separators=(',', ':')).encode()).hexdigest()


async def run(args):
    settings = load_settings(ROOT / '.env', require_file=True, settings_type=MigrationSettings)
    direct = parse_target(settings.database_direct_url.get_secret_value(), direct=True)
    require(await migrate(direct) == (15, 15))
    plan = json.loads((OUT / 'operator-plan.json').read_text())
    # Get issuer from the reviewed local non-secret deployment config, never identify by email.
    issuer = json.loads((OUT / 'disabled-environment.json').read_text())['Variables']['OIDC_ISSUER']
    require(issuer.startswith('https://cognito-idp.ap-southeast-1.amazonaws.com/'))
    async with direct_connection(direct) as connection:
        if args.action == 'list-principals':
            rows = await (await connection.execute('SELECT id FROM platform.principals WHERE issuer=%s AND active ORDER BY created_at DESC LIMIT 10', (issuer,))).fetchall()
            # Explicit local operator selection; no email/subject/token/CSRF is displayed.
            return {'passed': True, 'principal_ids_local_selection_only': [str(r[0]) for r in rows], 'applied': False}
        if args.action == 'prepare':
            require(args.principal_id is not None and not RECORD.exists())
            principal = UUID(args.principal_id)
            row = await (await connection.execute('SELECT 1 FROM platform.principals WHERE id=%s AND issuer=%s AND active', (principal, issuer))).fetchone()
            require(row is not None)
            database, session_user = await (await connection.execute('SELECT current_database(),session_user')).fetchone()
            operator = database_operator(database, session_user)
            require(operator['id'] != str(principal))
            marker = uuid4().hex[:12]
            record = {'schema': 1, 'account': plan['account'], 'stack_id': plan['stack_id'], 'target_sha256': target_fingerprint(direct), 'principal_id': str(principal), 'operator': operator, 'state': 'prepared',
                      'tenants': [{'id': str(uuid4()), 'membership': str(uuid4()), 'role_ids': [str(uuid4()), str(uuid4())], 'slug': 'fe-env-auth-' + marker + '-' + name.lower(),
                                   'name': 'Fiktif ' + name, 'roles': ['employee', 'knowledge_manager'] if name == 'Alpha' else ['knowledge_manager']} for name in ('Alpha', 'Beta')]}
            RECORD.write_text(json.dumps(record, indent=2), encoding='utf8')
            return {'passed': True, 'state': 'prepared', 'applied': False}
        record = json.loads(RECORD.read_text())
        require(record['account'] == plan['account'] and record['stack_id'] == plan['stack_id'])
        require(record['target_sha256'] == target_fingerprint(direct))
        require(all(t['slug'].startswith('fe-env-auth-') for t in record['tenants']))
        if not args.apply:
            return {'passed': True, 'action': args.action, 'applied': False, 'owned_tenants': 2}
        async with connection.transaction():
            if args.action == 'apply':
                require(record['state'] == 'prepared')
                database, session_user = await (await connection.execute('SELECT current_database(),session_user')).fetchone()
                operator = database_operator(database, session_user)
                require(operator == record['operator'] and operator['id'] != record['principal_id'])
                await connection.execute('INSERT INTO platform.principals(id,issuer,subject) VALUES(%s,%s,%s) ON CONFLICT(issuer,subject) DO NOTHING', (UUID(operator['id']), operator['issuer'], operator['subject']))
                operator_row = await (await connection.execute('SELECT id FROM platform.principals WHERE issuer=%s AND subject=%s AND active', (operator['issuer'], operator['subject']))).fetchone()
                require(operator_row == (UUID(operator['id']),))
                require(await (await connection.execute('SELECT 1 FROM platform.principals WHERE id=%s AND issuer=%s AND active', (UUID(record['principal_id']), issuer))).fetchone())
                for t in record['tenants']:
                    await connection.execute("INSERT INTO platform.tenants(id,slug,name,timezone) VALUES(%s,%s,%s,'Asia/Singapore')", (UUID(t['id']), t['slug'], t['name']))
                    await connection.execute('INSERT INTO platform.memberships(tenant_id,id,principal_id) VALUES(%s,%s,%s)', (UUID(t['id']), UUID(t['membership']), UUID(record['principal_id'])))
                    for role, rid in zip(t['roles'], t['role_ids']):
                        await connection.execute("INSERT INTO platform.membership_roles(tenant_id,id,membership_id,role,scope_kind,scope_ref,granted_by) VALUES(%s,%s,%s,%s,'tenant',%s,%s)", (UUID(t['id']), UUID(rid), UUID(t['membership']), role, t['id'], UUID(operator['id'])))
                        audit = {'schema': 1, 'operation': 'grant', 'channel': operator['channel'], 'operator_id': operator['id'], 'membership_id': t['membership'], 'role': role, 'scope_kind': 'tenant', 'scope_ref': t['id']}
                        request_hash = hashlib.sha256(json.dumps(audit, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                        await connection.execute("INSERT INTO platform.role_change_events(tenant_id,id,actor_id,membership_id,idempotency_key,request_hash,operation,role,scope_kind,scope_ref,result_version) VALUES(%s,%s,%s,%s,%s,%s,'grant',%s,'tenant',%s,1)", (UUID(t['id']), uuid4(), UUID(operator['id']), UUID(t['membership']), UUID(rid), request_hash, role, t['id']))
                record['state'] = 'applied'
            elif args.action == 'deactivate':
                require(record['state'] == 'applied')
                for t in record['tenants']:
                    found = await (await connection.execute('SELECT slug FROM platform.tenants WHERE id=%s', (UUID(t['id']),))).fetchone()
                    require(found == (t['slug'],))
                    await connection.execute('UPDATE platform.memberships SET active=false,access_revision=access_revision+1 WHERE tenant_id=%s AND id=%s AND principal_id=%s', (UUID(t['id']), UUID(t['membership']), UUID(record['principal_id'])))
                    await connection.execute('UPDATE platform.tenants SET active=false WHERE id=%s', (UUID(t['id']),))
                record['state'] = 'deactivated_retained_for_audit'
            else:
                raise ValueError('Unknown fixture action.')
        RECORD.write_text(json.dumps(record, indent=2), encoding='utf8')
        return {'passed': True, 'state': record['state'], 'applied': True, 'owned_tenants': 2, 'other_memberships_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('list-principals', 'prepare', 'apply', 'deactivate'))
    parser.add_argument('--principal-id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        with asyncio.Runner(loop_factory=database_loop_factory) as runner:
            print(json.dumps(runner.run(run(args))))
    except Exception as error:
        print(json.dumps({'passed': False, 'action': args.action, 'category': type(error).__name__}))
        print('Stop. Reconcile the exact owned fixture record and database before retrying.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
