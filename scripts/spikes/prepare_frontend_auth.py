"""Read-only AWS preflight and local operator deployment/rollback plan. Never applies changes."""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import boto3
from check_oidc_runtime import ROOT, STATE_FILE, safe_transport_logs
from deploy_identity_api import identity_status, RECORD, artifact_data

OUT = ROOT / 'local/fe-env-01'


def require(condition):
    if not condition:
        raise ValueError('Auth slice check rejected.')


def main():
    safe_transport_logs()
    try:
        state = json.loads(STATE_FILE.read_text(encoding='utf8'))
        session = boto3.Session(region_name='ap-southeast-1')
        function = identity_status(session, state, enabled=False, check_iam=False)
        current = json.loads(RECORD.read_text(encoding='utf8'))
        build = json.loads((OUT / 'build-proof.json').read_text(encoding='utf8'))
        artifact = ROOT / build['artifact']
        _, code_sha = artifact_data(artifact)
        require(build['passed'] and base64.b64decode(code_sha).hex() == build['zip_sha256'])
        rollback = Path(current['new_artifact'])
        require(rollback.is_file() and base64.b64encode(hashlib.sha256(rollback.read_bytes()).digest()).decode() == function['CodeSha256'])
        with closing(session.client('cloudformation')) as cloud, closing(session.client('lambda')) as client:
            stack = cloud.describe_stacks(StackName=state['stack_id'])['Stacks'][0]
            template = cloud.get_template(StackName=state['stack_id'])['TemplateBody']
            reserved = client.get_function_concurrency(FunctionName=state['function_arn']).get('ReservedConcurrentExecutions')
            # Same logical resources; execution must still be reviewed in the change set.
            cloud.validate_template(TemplateBody=(ROOT / 'infra/aws/frontend_auth_slice.yaml').read_text())
        require(isinstance(template, str))
        key = 'fe-env-01/' + build['zip_sha256'] + '.zip'
        parameters = [{ 'ParameterKey': p['ParameterKey'], 'UsePreviousValue': True } for p in stack['Parameters']]
        parameters = [p for p in parameters if p['ParameterKey'] != 'ArtifactKey']
        parameters += [{'ParameterKey': 'ArtifactKey', 'ParameterValue': key}, {'ParameterKey': 'FrontendEnabled', 'ParameterValue': 'true'}, {'ParameterKey': 'BrowserContextEnabled', 'ParameterValue': 'true'}]
        (OUT / 'parameters.json').write_text(json.dumps(parameters, indent=2), encoding='utf8')
        (OUT / 'rollback-template.yaml').write_text(template, encoding='utf8')
        previous = [{ 'ParameterKey': p['ParameterKey'], 'ParameterValue': p['ParameterValue'] } for p in stack['Parameters']]
        (OUT / 'rollback-parameters.json').write_text(json.dumps(previous, indent=2), encoding='utf8')
        variables = function['Environment']['Variables']
        require(not {'DATABASE_URL', 'DATABASE_DIRECT_URL', 'OIDC_CLIENT_SECRET', 'APP_AUTH_VERIFIER_KEY'} & variables.keys())
        # Local rollback contains only known non-secret Lambda config; secret stays in Secrets Manager.
        (OUT / 'rollback-environment.json').write_text(json.dumps({'Variables': {**variables, 'APP_AUTH_ENABLED': 'false', 'SPIKE_ENABLED': 'false'}}, indent=2), encoding='utf8')
        proposed = {**variables, 'APP_AUTH_DEMO_UI': 'false', 'APP_FRONTEND_ENABLED': 'true', 'APP_BROWSER_CONTEXT_ENABLED': 'true', 'APP_AUTH_ENABLED': 'false', 'SPIKE_ENABLED': 'false'}
        for enabled in (False, True):
            (OUT / ('enabled-environment.json' if enabled else 'disabled-environment.json')).write_text(json.dumps({'Variables': {**proposed, 'APP_AUTH_ENABLED': 'true' if enabled else 'false'}}, indent=2), encoding='utf8')
        plan = {'passed': True, 'stage': 'prepared_not_deployed', 'account': state['account'], 'region': state['region'],
                'stack_id': state['stack_id'], 'function_arn': state['function_arn'], 'origin': variables['PUBLIC_ORIGIN'], 'artifact': build['artifact'],
                'bucket': state['bucket'], 'artifact_key': key, 'new_code_sha256': code_sha, 'old_code_sha256': function['CodeSha256'],
                'old_revision_id': function['RevisionId'], 'role_arn': function['Role'], 'memory_mib': function['MemorySize'], 'timeout_seconds': function['Timeout'],
                'reserved_concurrency': reserved, 'callback_change_required': False, 'new_resources': 0, 'iam_change_expected': False,
                'change_set_name': 'fe-env-auth-' + uuid4().hex[:12], 'requires_operator_database_migration_to': 15,
                'template_sha256': hashlib.sha256((ROOT / 'infra/aws/frontend_auth_slice.yaml').read_bytes()).hexdigest()}
        (OUT / 'operator-plan.json').write_text(json.dumps(plan, indent=2), encoding='utf8')
        # Commands are written locally, never executed by this preparation tool.
        commands = f'''# William executes, Guru guides. Review docs/frontend/environment_auth.md first.
$taskPlan = Get-Content -Raw -LiteralPath './local/fe-env-01/operator-plan.json' | ConvertFrom-Json
aws sts get-caller-identity --query Account --output text
# Verify account equals the local plan; do not paste credentials or private outputs into chat.
aws s3 cp $taskPlan.artifact ("s3://" + $taskPlan.bucket + "/" + $taskPlan.artifact_key) --region ap-southeast-1
aws cloudformation create-change-set --stack-name $taskPlan.stack_id --change-set-name $taskPlan.change_set_name --change-set-type UPDATE --template-body file://infra/aws/frontend_auth_slice.yaml --parameters file://local/fe-env-01/parameters.json --capabilities CAPABILITY_IAM --region ap-southeast-1
aws cloudformation describe-change-set --stack-name $taskPlan.stack_id --change-set-name $taskPlan.change_set_name --region ap-southeast-1
# STOP: inspect change set. Only Function modified, no Add/Remove/Replacement/IAM changes.
# Execute only after William reviews the concrete changes:
# aws cloudformation execute-change-set --stack-name $taskPlan.stack_id --change-set-name $taskPlan.change_set_name --region ap-southeast-1
# aws cloudformation wait stack-update-complete --stack-name $taskPlan.stack_id --region ap-southeast-1
# Then run the disabled cloud canary from the runbook before enabling auth.
'''
        (OUT / 'operator-commands.ps1').write_text(commands, encoding='utf8')
        print(json.dumps({'passed': True, 'stage': plan['stage'], 'new_resources': 0, 'iam_change_expected': False,
                          'account_suffix': state['account'][-4:], 'reserved_concurrency': reserved, 'callback_change_required': False, 'aws_mutations': False}))
    except Exception as error:
        print(json.dumps({'passed': False, 'category': type(error).__name__, 'aws_mutations': False}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
