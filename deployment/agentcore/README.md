# AgentCore integration

The platform worker can execute its policy-answer and reimbursement-preparation jobs through
Amazon Bedrock AgentCore Runtime. The existing LangGraph turns, PostgreSQL checkpoints,
source-access checks, model reservations, and human-confirmation flow stay in use.

```text
Conversation / reimbursement workspace
    -> durable agent job -> platform worker -> AgentCore Runtime
    -> LangGraph -> AgentCore Gateway -> private tools Lambda
    -> current policy access / shared editable draft
```

## Integration map

- [Runtime entry point](../../backend/src/enterprise_ai_platform/agentcore_runtime.py):
  AgentCore HTTP `/invocations` and `/ping`, with serial execution per runtime session.
- [Gateway target](../../backend/src/enterprise_ai_platform/agentcore_tools.py):
  private Lambda exposing policy search, version-access checks, and draft preparation.
- [AWS adapters](../../backend/src/enterprise_ai_platform/integrations/agentcore/clients.py):
  signed MCP calls, Runtime invocation, scoped execution memory, and trace spans.
- [Worker](../../backend/src/enterprise_ai_platform/deployment_worker.py):
  selects local or AgentCore execution through explicit process configuration.

## Tools and authority

| Tool | Owning application operation |
| --- | --- |
| `search_policy` | Retrieve current accessible policy passages for the admitted question. |
| `get_policy_version` | Recheck access to the exact retrieved document versions. |
| `create_reimbursement_draft` | Create, populate, and preview the admitted editable draft. |

Gateway uses IAM authorization. The Runtime execution role can invoke the configured Gateway;
the Gateway role can invoke the configured tools Lambda. Original user and session authority
are resolved from the durable job. The Lambda rechecks the job lease and epoch before using
the owning application RPCs. Source attestations and draft content are rebuilt from current
application records. Confirmation and reviewer decisions remain in the application UI.

Runtime supplies its AgentCore-managed workload identity. Application sessions continue to use
the existing Cognito/BFF flow. No browser cookie or provider credential is sent to the model.

## Memory and tracing

Short-term execution context is scoped to hashed tenant, actor, and original-session identifiers.
It stores run references and workflow phase. Conversation text, policy text, attachments, and
credentials remain in their owning stores. The existing conversation pipeline supplies question
history and current sources.

OpenTelemetry adds Runtime and Gateway tool spans without recording private content. Enable
CloudWatch Transaction Search for the AgentCore trace view using the AWS setup guide below.
Review this account-level setting alongside the deployment configuration.

## Local preparation

Use the backend's optional `agentcore` dependency group for the worker and Runtime image.
Render infrastructure definitions without contacting AWS:

```powershell
python deployment/agentcore/render.py --out local/agentcore/templates
```

The [foundation template](templates/foundation.json) defines private image repositories and
secret containers. The [services template](templates/services.json) defines Runtime, Gateway,
Memory, the tools Lambda, scoped execution roles, and log retention.

Runtime and tools images are built from the repository root using
[`Dockerfile.runtime`](Dockerfile.runtime) and [`Dockerfile.tools`](Dockerfile.tools).
Both use ARM64. Deploy image digests rather than mutable tags.

## Environment and rollout

Use Singapore (`ap-southeast-1`) for this integration. The operator configures:

- Restricted platform database runtime credentials and the registered agent executors.
- Current platform migrations and the independent reference-service migrations.
- An HTTPS reference-service endpoint reachable from Runtime and Lambda, with its signed
  caller configuration and actor-verification route back to the platform.
- Cognito callback/public origin and the platform's knowledge, conversations, and draft services.
- The exact Nova Pro inference-profile/destination ARNs and the configured embedding model ARN.

Store Runtime and tools configuration in separate Secrets Manager secrets. Each secret contains
a JSON object of environment-name/string-value pairs. Supported names are `APP_*`, `OIDC_*`,
`REIMBURSEMENT_*`, `DATABASE_URL`, `PUBLIC_ORIGIN`, and `EXECUTORS_JSON`. Exclude migration/direct
database credentials. Include only registered agent executors in the appropriate secret.

Apply the foundation stack, populate its private configuration, build/push the images, then
apply the services stack with the reviewed image digests and secret ARNs. Model execution is
an explicit stack parameter. The selected Nova Pro profile retains its APAC routing configuration.

Grant the existing worker role `bedrock-agentcore:InvokeAgentRuntime` and
`bedrock-agentcore:StopRuntimeSession` on the exact Runtime ARN, then configure its process:

```text
EAI_AGENT_EXECUTION_BACKEND=agentcore
EAI_AGENTCORE_RUNTIME_ARN=<RuntimeArn stack output>
```

Keep the same registered worker credential, source authority, and model-budget configuration.
The worker starts a Runtime request only for `agent.resume`; other job types keep their owning
execution paths. File parsing initializes only when a file job is processed.

## Rollback and cleanup

Set the worker's execution backend to `local` and let an in-flight Runtime call settle before
changing execution configuration. A lost response is reconciled through the original job/run;
the adapter does not automatically repeat the invocation. Runtime sessions stop after a successful
response and have an idle timeout as a fallback. PostgreSQL preserves checkpoints and draft state.

Delete the services stack after stopping the worker. Retain or remove configuration secrets and
images according to the selected environment lifecycle. Cognito, application databases, source
documents, and the independent business service belong to their existing deployment configuration.

## Operator verification

Run the policy-question and draft-preparation journeys serially. Check current source access,
original-session revocation, stale job/epoch rejection, cross-tenant denial, draft replay, and
the UI confirmation boundary. Follow the shared [resource policy](../../RESOURCE_SAFETY_POLICY.md):
targeted checks first, files serial, one validation job at a time, and at most two workers total.

## AWS references

- [Runtime HTTP contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
- [Gateway Lambda targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html)
- [Runtime identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html)
- [Observability setup](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
