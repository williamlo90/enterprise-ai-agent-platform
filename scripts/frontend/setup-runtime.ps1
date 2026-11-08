[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$version = [IO.File]::ReadAllText((Join-Path $repoRoot 'frontend/.node-version')).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid pinned Node version.' }
if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or
    [Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
    throw 'This portable runtime helper supports Windows x64 only.'
}

$runtimeRoot = Join-Path $repoRoot 'local/frontend-runtime'
$distribution = "node-v$version-win-x64"
$runtimeDirectory = Join-Path $runtimeRoot $distribution
$nodePath = Join-Path $runtimeDirectory 'node.exe'
if (Test-Path -LiteralPath $nodePath) {
    $actual = & $nodePath --version
    if ($LASTEXITCODE -ne 0 -or $actual -ne "v$version") { throw 'Portable runtime version mismatch.' }
    Write-Output "Portable Node $version is ready; global PATH unchanged."
    exit 0
}

[void](New-Item -ItemType Directory -Path $runtimeRoot -Force)
$archiveName = "$distribution.zip"
$archivePath = Join-Path $runtimeRoot $archiveName
$sumsPath = Join-Path $runtimeRoot "SHASUMS256-v$version.txt"
$origin = "https://nodejs.org/dist/v$version"
Invoke-WebRequest -Uri "$origin/SHASUMS256.txt" -OutFile $sumsPath
$pattern = '^([a-f0-9]{64})\s+' + [regex]::Escape($archiveName) + '$'
$matchesForArchive = @(Get-Content -LiteralPath $sumsPath | Where-Object { $_ -match $pattern })
if ($matchesForArchive.Count -ne 1) { throw 'Pinned runtime archive is absent from official checksums.' }
$expectedHash = [regex]::Match($matchesForArchive[0], $pattern).Groups[1].Value
if (-not (Test-Path -LiteralPath $archivePath)) {
    Invoke-WebRequest -Uri "$origin/$archiveName" -OutFile $archivePath
}
$actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) { throw 'Runtime checksum mismatch. No extraction was performed.' }

# Check every resolved extraction target before allowing the archive to touch disk.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    $allowedRoot = [IO.Path]::GetFullPath($runtimeDirectory) + [IO.Path]::DirectorySeparatorChar
    foreach ($entry in $archive.Entries) {
        $target = [IO.Path]::GetFullPath((Join-Path $runtimeRoot $entry.FullName))
        if ($target -ne [IO.Path]::GetFullPath($runtimeDirectory) -and
            -not $target.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Runtime archive contains an unexpected extraction path.'
        }
    }
} finally { $archive.Dispose() }
Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeRoot -Force
$actual = & $nodePath --version
if ($LASTEXITCODE -ne 0 -or $actual -ne "v$version") { throw 'Extracted runtime failed version verification.' }
Write-Output "Portable Node $version installed; official SHA256 $actualHash verified; global PATH unchanged."
