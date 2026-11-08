param([Parameter(Mandatory=$true)][string]$Cluster)
$ErrorActionPreference = 'Stop'
& aws eks update-kubeconfig --region ap-southeast-1 --name $Cluster
if ($LASTEXITCODE -ne 0) { throw 'Cluster selection failed.' }
& kubectl -n enterprise-ai scale deployment/worker --replicas=0
if ($LASTEXITCODE -ne 0) { throw 'Could not hold workers.' }
foreach ($name in @('platform','reference','web')) {
 & kubectl -n enterprise-ai rollout undo "deployment/$name"
 if ($LASTEXITCODE -ne 0) { throw "Rollback stopped at $name." }
}
Write-Output 'Application rollback requested; database migrations are NOT reversed.'
