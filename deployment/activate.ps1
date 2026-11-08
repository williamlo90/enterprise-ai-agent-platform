param([Parameter(Mandatory=$true)][string]$Target,[switch]$EnableWorker)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$config = Get-Content -LiteralPath $Target -Raw | ConvertFrom-Json
if ($config.region -ne 'ap-southeast-1' -or $config.namespace -ne 'enterprise-ai') { throw 'Unexpected deployment target.' }
# Operator executes this script. It does not migrate or provision credentials.
& aws eks update-kubeconfig --region $config.region --name $config.cluster
if ($LASTEXITCODE -ne 0) { throw 'Could not select the operator cluster.' }
& python "$PSScriptRoot/render.py" --config $Target --out "$PSScriptRoot/generated/release.json"
if ($LASTEXITCODE -ne 0) { throw 'Render failed; no manifest applied.' }
& kubectl apply -f "$PSScriptRoot/generated/release.json"
if ($LASTEXITCODE -ne 0) { throw 'Apply failed; inspect locally.' }
if ($EnableWorker) {
 & kubectl -n enterprise-ai scale deployment/worker --replicas=1
 if ($LASTEXITCODE -ne 0) { throw 'Worker activation failed.' }
}
Write-Output 'Manifests applied by operator. Acceptance checks remain with William.'
