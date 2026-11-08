# Local setup

## Prerequisites

Use Python 3.13 and uv. Frontend Node and npm versions are pinned in
[`package.json`](../frontend/package.json) and [`.node-version`](../frontend/.node-version).
Each Python service owns its lockfile and environment.

## Platform bootstrap

From the repository root:

```powershell
python -m uv sync --locked --project backend
python -m uv run --locked --project backend enterprise-ai-api --env-file .env.example --check-config
python -m uv run --locked --project backend enterprise-ai-api --env-file .env.example
```

Open `http://127.0.0.1:8000/docs`. Health endpoints are `/health/live` and `/health/ready`.
The supplied defaults leave identity, databases, model execution, and business integrations disabled.
This mode is useful for exploring the API foundation; it does not seed an authenticated workspace.

For private configuration, copy `.env.example` to `.env` only if `.env` does not already exist.
Run the API with `--env-file .env`. Process environment values override file values.
Never put credentials in command arguments or commit the private file.

## Frontend shell

On Windows, from the repository root:

```powershell
& ./scripts/frontend/setup-runtime.ps1
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('ci', '--ignore-scripts')
& ./scripts/frontend/run-npm.ps1 -NpmArguments @('run', 'dev')
```

The helper installs the pinned portable runtime under ignored `local/` and leaves the system
installation unchanged. Open `http://127.0.0.1:5173`. The default shell makes no authenticated
business calls. On other platforms, use the pinned runtime and run `npm ci --ignore-scripts`
then `npm run dev` from `frontend/`.

## Authenticated workspace

Full workflows require configured Cognito identity, restricted platform and reference-service
database credentials, schema migrations, tenant membership, private storage, and business policy.
Frontend auth is enabled with `EAI_FRONTEND_AUTH_ENABLED=true` in the frontend process environment.
Secure session cookies require HTTPS and an exact callback/public-origin configuration.
Frontend and browser API must share an origin; the private service routes remain internal.

Follow the [deployment runbook](../deployment/README.md) for the connected environment. Setup of
these integrations is explicit operator work. API startup does not migrate databases or invoke
models automatically. Model use requires a configured provider and explicit activation.

## Next steps

- Read the [backend guide](../backend/README.md) and [reference-service guide](../reference_service/README.md).
- Browse the [product tour](product-tour/README.md) without starting services.
- Use the [verification guide](../TEST_PLAN.md) for deliberate checks; setup does not run them.
