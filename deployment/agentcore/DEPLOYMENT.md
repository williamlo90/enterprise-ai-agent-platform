# AgentCore deployment

The platform's policy-answer and reimbursement-preparation workflow was executed on
Amazon Bedrock AgentCore in Singapore on 10 November 2026.

Policy questions returned answers with source citations. Draft preparation used the
selected trip and receipts, created an editable reimbursement draft, and paused for
human confirmation.

| Component | Deployment |
| --- | --- |
| Runtime | ARM64 Python with direct-code packaging; the existing LangGraph workflows and PostgreSQL checkpoints. |
| Gateway | IAM-authorized MCP tools for policy retrieval, source rechecks, and draft preparation. |
| Memory | Execution context scoped to the tenant, actor, and original user session. |
| Access | Original session and role checks resolved from the durable job and enforced by the owning applications. |
| Configuration | Separate Secrets Manager configurations and scoped execution roles. |

The reimbursement source adapter uses its own restricted database credential.
Confirmation and approval remain explicit application actions.

Deployment resources were removed after the execution records were captured.

- [Integration and deployment guide](README.md)
- [Runtime entry point](../../backend/src/enterprise_ai_platform/agentcore_runtime.py)
- [Gateway tools](../../backend/src/enterprise_ai_platform/agentcore_tools.py)
- [Session and transport adapters](../../backend/src/enterprise_ai_platform/integrations/agentcore/clients.py)
