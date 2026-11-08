# Frontend workspace

React, TypeScript, and Vite provide a bilingual company workspace for conversations, source
inspection, document governance, reimbursement preparation, approvals, and scoped administration.
Manual and assisted claim preparation converge on a shared review and confirmation form.

The interface supports light/dark preferences and responsive layouts. Business authority comes
from the API; visible controls do not replace server authorization. Company changes and logout
clear private state. Only locale and theme preferences are persisted as browser preferences.

## Setup

Use the exact Node/npm versions in [`package.json`](package.json) and [`.node-version`](.node-version).
On Windows, run from the repository root:

```powershell
& ./scripts/frontend/setup-runtime.ps1
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('ci', '--ignore-scripts')
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('run', 'dev')
```

The runtime helper downloads the official pinned runtime, verifies its checksum, and installs it
under ignored `local/`. It leaves the system Node installation unchanged. Open
`http://127.0.0.1:5173`. With an existing matching runtime on another platform, run
`npm ci --ignore-scripts` and `npm run dev` inside `frontend/`.

## Authentication

Auth is opt-in through `EAI_FRONTEND_AUTH_ENABLED=true` in the frontend process environment.
The default public shell makes no authenticated API requests. Vite does not load `.env` files or
expose arbitrary environment values to the browser.

Authenticated flows require an HTTPS same-origin API, exact Cognito callback/public-origin
configuration, and configured backend services. For existing local TLS files, `EAI_LOCAL_HTTPS=true`
uses the ignored certificate paths declared in [`vite.config.ts`](vite.config.ts); it does not
generate certificates. The production web container enables frontend auth during its build.
Follow [local setup](../docs/GETTING_STARTED.md) and the [deployment runbook](../deployment/README.md).

## Commands

Run commands individually, from the repository root:

```powershell
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('run', 'typecheck')
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('run', 'lint')
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('run', 'test:unit')
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('run', 'build')
```

These are available developer commands, not checks automatically performed during setup. Browser
and API integration checks require explicit opt-in and an HTTPS integration origin. Respect the
[resource policy](../RESOURCE_SAFETY_POLICY.md) and [verification guide](../TEST_PLAN.md).

See the [product tour](../docs/product-tour/README.md) for application views. Adapted UI component
and dependency attribution is preserved in [third-party notices](THIRD_PARTY_NOTICES.md).
