[CmdletBinding()]
param([Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string[]]$NpmArguments)

# Pass an explicit array: PowerShell may consume a bare `--` in trailing arguments.
# Reject unbound arguments before npm can silently interpret test flags as npm config.
if ($args.Count -gt 0) { throw 'Use -NpmArguments with one explicit array; see frontend/README.md.' }

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$frontendRoot = Join-Path $repoRoot 'frontend'
$version = [IO.File]::ReadAllText((Join-Path $frontendRoot '.node-version')).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid pinned Node version.' }
$runtimeDirectory = Join-Path $repoRoot "local/frontend-runtime/node-v$version-win-x64"
$nodePath = Join-Path $runtimeDirectory 'node.exe'
$npmPath = Join-Path $runtimeDirectory 'node_modules/npm/bin/npm-cli.js'
if (-not (Test-Path -LiteralPath $nodePath) -or -not (Test-Path -LiteralPath $npmPath)) {
    throw 'Portable runtime missing. Run scripts/frontend/setup-runtime.ps1 first.'
}
$actual = & $nodePath --version
if ($LASTEXITCODE -ne 0 -or $actual -ne "v$version") { throw 'Portable runtime version mismatch.' }
if (-not $NpmArguments -or $NpmArguments.Count -eq 0) { throw 'Provide an npm command.' }

$previousPath = $env:PATH
$previousBrowserDownload = $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD
$exitCode = 1
Push-Location -LiteralPath $frontendRoot
try {
    $env:PATH = $runtimeDirectory + [IO.Path]::PathSeparator + $previousPath
    $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = '1'
    & $nodePath $npmPath @NpmArguments
    $exitCode = $LASTEXITCODE
} finally {
    $env:PATH = $previousPath
    $env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = $previousBrowserDownload
    Pop-Location
}
exit $exitCode
