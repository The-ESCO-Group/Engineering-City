[CmdletBinding()]
param(
    [string] $Version = "0.1.0",
    [string] $OutputDirectory = "",
    [switch] $SkipBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "common.ps1")

Assert-ProductVersion -Version $Version
$repoRoot = Get-ProductRepoRoot
$tauriConfigPath = Join-Path $repoRoot "desktop\src-tauri\tauri.conf.json"
$cargoManifestPath = Join-Path $repoRoot "desktop\src-tauri\Cargo.toml"
$configuredVersion = (Get-Content -LiteralPath $tauriConfigPath -Raw | ConvertFrom-Json).version
$cargoManifest = Get-Content -LiteralPath $cargoManifestPath -Raw
$cargoVersion = if ($cargoManifest -match '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$') { $Matches.version } else { "" }
if ($Version -ne $configuredVersion -or $Version -ne $cargoVersion) {
    throw "Package version '$Version' must match tauri.conf.json ('$configuredVersion') and Cargo.toml ('$cargoVersion')."
}
if ($SkipBuild) {
    throw "-SkipBuild is not permitted for release packaging because stale installers cannot be attributed to this invocation."
}
$bundleRoot = Join-Path $repoRoot "desktop\src-tauri\target\release\bundle"
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repoRoot "dist\desktop" }
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null

$buildStartedUtc = [DateTime]::UtcNow
& (Join-Path $PSScriptRoot "build-desktop.ps1")

if (-not (Test-Path -LiteralPath $bundleRoot -PathType Container)) {
    throw "Desktop bundle directory '$bundleRoot' does not exist. Run build-desktop.ps1 first."
}

$installers = Get-ChildItem -LiteralPath $bundleRoot -Recurse -File | Where-Object {
    $_.Extension.ToLowerInvariant() -in @(".msi", ".exe") -and
    $_.LastWriteTimeUtc -ge $buildStartedUtc.AddSeconds(-3)
}
if (-not $installers) {
    throw "No installer produced by the current build invocation was found beneath '$bundleRoot'."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$packageName = "esco-engineering-city-$Version-windows-$stamp"
$staging = Join-Path $outputRoot $packageName
$zipPath = Join-Path $outputRoot "$packageName.zip"
if ((Test-Path -LiteralPath $staging) -or (Test-Path -LiteralPath $zipPath)) {
    throw "Package target already exists; refusing to overwrite '$packageName'."
}
[System.IO.Directory]::CreateDirectory($staging) | Out-Null

$artifactRows = @()
foreach ($installer in $installers) {
    $relative = [System.IO.Path]::GetRelativePath($bundleRoot, $installer.FullName)
    $destination = Join-Path $staging $relative
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $destination)) | Out-Null
    Copy-Item -LiteralPath $installer.FullName -Destination $destination
    $hash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
    $artifactRows += [ordered]@{
        path = $relative.Replace('\', '/')
        bytes = (Get-Item -LiteralPath $destination).Length
        sha256 = $hash
    }
}

$git = Get-Command git -ErrorAction SilentlyContinue | Select-Object -First 1
$commit = "unavailable"
$sourceTree = "unavailable"
$sourceStatusHash = "unavailable"
if ($git) {
    Push-Location -LiteralPath $repoRoot
    try {
        $commit = (& $git.Source rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0) { $commit = "unavailable" }
        $trackedStatus = ((& $git.Source status --porcelain=v1 --untracked-files=no) | Out-String).Trim()
        if ($LASTEXITCODE -eq 0) {
            $sourceTree = if ($trackedStatus) { "tracked_changes_present" } else { "clean" }
            $statusBytes = [Text.Encoding]::UTF8.GetBytes($trackedStatus)
            $sourceStatusHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($statusBytes)).ToLowerInvariant()
        }
    }
    finally { Pop-Location }
}

$manifest = [ordered]@{
    product = "ESCO Engineering City"
    version = $Version
    platform = "windows"
    created_utc = [DateTimeOffset]::UtcNow.ToString("o")
    source_commit = $commit
    source_tree = $sourceTree
    source_tracked_status_sha256 = $sourceStatusHash
    verification = "packaged_not_import_verified"
    artifacts = $artifactRows
    safety_boundary = "No controller upload, download, online edit, force, or live equipment control is included."
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $staging "package-manifest.json") -Encoding utf8
$artifactRows | ForEach-Object { "$($_.sha256)  $($_.path)" } | Set-Content -LiteralPath (Join-Path $staging "SHA256SUMS.txt") -Encoding ascii
@"
ESCO Engineering City is local engineering-review software.

This package does not claim Rockwell import verification, human approval, or released
engineering status. It contains no controller upload/download, online-edit, force, or
live equipment-control capability. Review package-manifest.json and SHA256SUMS.txt
before installation or internal distribution.
"@ | Set-Content -LiteralPath (Join-Path $staging "SECURITY-NOTICE.txt") -Encoding utf8

Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zipPath -CompressionLevel Optimal
$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Packaged Windows desktop release: $zipPath"
Write-Host "SHA-256: $zipHash"
