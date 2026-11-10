"""Render CloudFormation templates locally. This command makes no AWS calls."""
import argparse
import json
from pathlib import Path


def ref(name): return {'Ref': name}
def attr(name, field): return {'Fn::GetAtt': [name, field]}
def sub(value): return {'Fn::Sub': value}
def policy(statements): return {'Version': '2012-10-17', 'Statement': statements}
def allow(actions, resource): return {'Effect': 'Allow', 'Action': actions, 'Resource': resource}


def role(service, statements):
    return {'Type': 'AWS::IAM::Role', 'Properties': {
        'AssumeRolePolicyDocument': policy([{'Effect': 'Allow', 'Principal': {'Service': service},
            'Action': 'sts:AssumeRole', 'Condition': {'StringEquals': {'aws:SourceAccount': ref('AWS::AccountId')}}}]),
        'Policies': [{'PolicyName': 'scoped-execution', 'PolicyDocument': policy(statements)}]}}


def templates():
    foundation = {'AWSTemplateFormatVersion': '2010-09-09',
        'Description': 'Enterprise AI AgentCore image repositories and private runtime configuration.',
        'Parameters': {'Prefix': {'Type': 'String', 'Default': 'enterprise-ai-agent', 'AllowedPattern': '[a-z][a-z0-9-]{2,35}'}},
        'Resources': {}, 'Outputs': {}}
    for name in ('Runtime', 'Tools'):
        repository = name + 'Repository'
        secret = name + 'Configuration'
        foundation['Resources'][repository] = {'Type': 'AWS::ECR::Repository', 'Properties': {
            'RepositoryName': sub('${Prefix}-' + name.lower()), 'ImageTagMutability': 'IMMUTABLE',
            'ImageScanningConfiguration': {'ScanOnPush': False},
            'LifecyclePolicy': {'LifecyclePolicyText': json.dumps({'rules': [{'rulePriority': 1,
                'description': 'Keep recent immutable images', 'selection': {'tagStatus': 'any',
                'countType': 'imageCountMoreThan', 'countNumber': 3}, 'action': {'type': 'expire'}}]})}}}
        foundation['Resources'][secret] = {'Type': 'AWS::SecretsManager::Secret', 'Properties': {
            'Name': sub('${Prefix}/' + name.lower()), 'Description': name + ' restricted runtime configuration'}}
        foundation['Outputs'][name + 'RepositoryUri'] = {'Value': attr(repository, 'RepositoryUri')}
        foundation['Outputs'][name + 'SecretArn'] = {'Value': ref(secret)}

    parameters = {
        'Prefix': {'Type': 'String', 'Default': 'enterprise-ai-agent', 'AllowedPattern': '[a-z][a-z0-9-]{2,35}'},
        'RuntimeName': {'Type': 'String', 'Default': 'enterprise_ai_agent', 'AllowedPattern': '[A-Za-z][A-Za-z0-9_]{0,47}'},
        'RuntimeImageUri': {'Type': 'String', 'AllowedPattern': '.+@sha256:[a-f0-9]{64}'},
        'ToolsImageUri': {'Type': 'String', 'AllowedPattern': '.+@sha256:[a-f0-9]{64}'},
        'RuntimeSecretArn': {'Type': 'String'}, 'ToolsSecretArn': {'Type': 'String'},
        'ModelResourceArns': {'Type': 'CommaDelimitedList', 'Description': 'Exact APAC Nova Pro profile and destination model ARNs plus Singapore Cohere embedding ARN.'},
        'EnableModelExecution': {'Type': 'String', 'Default': 'false', 'AllowedValues': ['false', 'true']},
    }
    logs = sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/bedrock-agentcore/runtimes/${RuntimeName}*')
    runtime_statements = [
        allow(['secretsmanager:GetSecretValue'], ref('RuntimeSecretArn')),
        allow(['bedrock-agentcore:InvokeGateway'], attr('Gateway', 'GatewayArn')),
        allow(['bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents'], attr('Memory', 'MemoryArn')),
        allow(['bedrock:InvokeModel'], ref('ModelResourceArns')),
        allow(['bedrock:GetAccountDataRetention', 'bedrock:GetModelInvocationLoggingConfiguration'], '*'),
        allow(['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'], logs),
        allow(['ecr:GetAuthorizationToken'], '*'),
        allow(['ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'], sub('arn:${AWS::Partition}:ecr:${AWS::Region}:${AWS::AccountId}:repository/${Prefix}-runtime')),
        allow(['xray:PutTraceSegments', 'xray:PutTelemetryRecords'], '*'),
        {'Effect': 'Deny', 'Action': ['bedrock-agentcore:GetWorkloadAccessTokenForUserId', 'bedrock-agentcore:InvokeAgentRuntimeForUser'], 'Resource': '*'},
    ]
    tools_log = sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/${Prefix}-tools:*')
    resources = {
        'RuntimeRole': role('bedrock-agentcore.amazonaws.com', runtime_statements),
        'ToolsRole': role('lambda.amazonaws.com', [allow(['secretsmanager:GetSecretValue'], ref('ToolsSecretArn')),
            allow(['logs:CreateLogStream', 'logs:PutLogEvents'], tools_log),
            allow(['xray:PutTraceSegments', 'xray:PutTelemetryRecords'], '*')]),
        'GatewayRole': role('bedrock-agentcore.amazonaws.com', [allow(['lambda:InvokeFunction'],
            sub('arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${Prefix}-tools'))]),
        'Memory': {'Type': 'AWS::BedrockAgentCore::Memory', 'Properties': {
            'Name': sub('${RuntimeName}_memory'), 'EventExpiryDuration': 7,
            'Description': 'Tenant/actor/original-session execution context; no raw content.'}},
        'Gateway': {'Type': 'AWS::BedrockAgentCore::Gateway', 'Properties': {
            'Name': sub('${Prefix}-gateway'), 'AuthorizerType': 'AWS_IAM', 'ProtocolType': 'MCP',
            'RoleArn': attr('GatewayRole', 'Arn'), 'Description': 'Policy retrieval and draft preparation tools.'}},
        'ToolsLogGroup': {'Type': 'AWS::Logs::LogGroup', 'Properties': {
            'LogGroupName': sub('/aws/lambda/${Prefix}-tools'), 'RetentionInDays': 7}},
        'Tools': {'Type': 'AWS::Lambda::Function', 'DependsOn': ['ToolsLogGroup'], 'Properties': {
            'FunctionName': sub('${Prefix}-tools'), 'PackageType': 'Image', 'Architectures': ['arm64'],
            'Code': {'ImageUri': ref('ToolsImageUri')}, 'Role': attr('ToolsRole', 'Arn'),
            'MemorySize': 512, 'Timeout': 30, 'ReservedConcurrentExecutions': 1,
            'TracingConfig': {'Mode': 'Active'}, 'Environment': {'Variables': {
                'EAI_AGENTCORE_SECRET_ARN': ref('ToolsSecretArn'), 'EAI_AGENTCORE_GATEWAY_ID': attr('Gateway', 'GatewayIdentifier')}}}},
        'Runtime': {'Type': 'AWS::BedrockAgentCore::Runtime', 'Properties': {
            'AgentRuntimeName': ref('RuntimeName'), 'RoleArn': attr('RuntimeRole', 'Arn'),
            'AgentRuntimeArtifact': {'ContainerConfiguration': {'ContainerUri': ref('RuntimeImageUri')}},
            'NetworkConfiguration': {'NetworkMode': 'PUBLIC'}, 'ProtocolConfiguration': 'HTTP',
            'LifecycleConfiguration': {'IdleRuntimeSessionTimeout': 60, 'MaxLifetime': 900},
            'EnvironmentVariables': {'AWS_REGION': ref('AWS::Region'), 'EAI_AGENTCORE_SECRET_ARN': ref('RuntimeSecretArn'),
                'EAI_AGENTCORE_GATEWAY_URL': attr('Gateway', 'GatewayUrl'), 'EAI_AGENTCORE_MEMORY_ID': attr('Memory', 'MemoryId'),
                'EAI_ENABLE_MODEL_EXECUTION': ref('EnableModelExecution'), 'EAI_FICTIONAL_APAC_ENABLED': ref('EnableModelExecution'),
                'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT': 'false', 'TRACELOOP_TRACE_CONTENT': 'false'}}},
        'RuntimeLogGroup': {'Type': 'AWS::Logs::LogGroup', 'Properties': {
            'LogGroupName': sub('/aws/bedrock-agentcore/runtimes/${Runtime.AgentRuntimeId}-DEFAULT'), 'RetentionInDays': 7}},
    }
    string = {'Type': 'string'}
    binding_schema = {'Type': 'object', 'Properties': {'id': string, 'tenant_id': string, 'worker_id': string,
        'generation': {'Type': 'integer'}, 'epoch': {'Type': 'integer'}},
        'Required': ['id', 'tenant_id', 'worker_id', 'generation', 'epoch']}
    argument_schemas = {
        'search_policy': {'Type': 'object', 'Properties': {'operation': string, 'query': string,
            'vector': {'Type': 'array', 'Items': {'Type': 'number'}}, 'mode': string},
            'Required': ['operation', 'query', 'vector', 'mode']},
        'get_policy_version': {'Type': 'object', 'Properties': {'operation': string,
            'items': {'Type': 'array', 'Items': {'Type': 'object'}}}, 'Required': ['operation', 'items']},
        'create_reimbursement_draft': {'Type': 'object', 'Properties': {'operation': string, 'content': {'Type': 'object'}},
            'Required': ['operation']},
    }
    tools = [{'Name': name, 'Description': description, 'InputSchema': {'Type': 'object',
        'Properties': {'binding': binding_schema, 'arguments': argument_schemas[name]}, 'Required': ['binding', 'arguments']}}
        for name, description in [('search_policy', 'Retrieve accessible policy passages for the admitted question.'),
            ('get_policy_version', 'Recheck current access to the exact retrieved policy versions.'),
            ('create_reimbursement_draft', 'Prepare the admitted editable draft and preview; human confirmation stays in the application.')]]
    resources['ToolsTarget'] = {'Type': 'AWS::BedrockAgentCore::GatewayTarget', 'Properties': {
        'GatewayIdentifier': attr('Gateway', 'GatewayIdentifier'), 'Name': 'platform',
        'CredentialProviderConfigurations': [{'CredentialProviderType': 'GATEWAY_IAM_ROLE'}],
        'TargetConfiguration': {'Mcp': {'Lambda': {'LambdaArn': attr('Tools', 'Arn'), 'ToolSchema': {'InlinePayload': tools}}}}}}
    services = {'AWSTemplateFormatVersion': '2010-09-09', 'Description': 'Enterprise AI AgentCore Runtime, Gateway, Memory and private tools.',
        'Parameters': parameters, 'Resources': resources, 'Outputs': {
            'RuntimeArn': {'Value': attr('Runtime', 'AgentRuntimeArn')}, 'GatewayUrl': {'Value': attr('Gateway', 'GatewayUrl')},
            'MemoryId': {'Value': attr('Memory', 'MemoryId')}, 'RuntimeRoleArn': {'Value': attr('RuntimeRole', 'Arn')},
            'WorkerInvokeResource': {'Value': attr('Runtime', 'AgentRuntimeArn')}}}
    return foundation, services


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, template in zip(('foundation.json', 'services.json'), templates()):
        (args.out / name).write_text(json.dumps(template, indent=2) + '\n', encoding='utf-8')
    print('Local CloudFormation templates rendered. No AWS operations performed.')


if __name__ == '__main__':
    main()
