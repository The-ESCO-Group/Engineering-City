[CmdletBinding()]
param(
    [ValidateSet("nsis", "msi", "all")]
    [string] $Bundle = "all",

    [string] $BackendUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-ProductRepoRoot
$desktop = Join-Path $repoRoot "desktop\src-tauri"
$cargo = Get-ProductCommandPath -Name "cargo" -InstallHint "Install the stable Rust toolchain."
$null = Get-ProductCommandPath -Name "rustc" -InstallHint "Install the stable Rust toolchain."
$null = Assert-ProductNodeVersion
$null = Get-ProductCommandPath -Name "npm.cmd" -InstallHint "Install npm with Node.js."
$tauriCli = Get-ProductLocalTauriCli
Assert-NoFrontendSecrets
$backendBase = ConvertTo-ProductLoopbackUrl -Url $BackendUrl

Invoke-ProductNative -FilePath $tauriCli.NodePath -ArgumentList @($tauriCli.CliPath, "--version") -WorkingDirectory $desktop -Description "Validate repository-local Tauri CLI $($tauriCli.Version)"

Invoke-ProductNative -FilePath $cargo -ArgumentList @("fmt", "--", "--check") -WorkingDirectory $desktop -Description "Check desktop Rust formatting"
Invoke-ProductNative -FilePath $cargo -ArgumentList @("test", "--locked") -WorkingDirectory $desktop -Description "Run desktop Rust tests"

$arguments = @($tauriCli.CliPath, "build")
if ($Bundle -ne "all") { $arguments += @("--bundles", $Bundle) }
$priorApiBase = [Environment]::GetEnvironmentVariable("VITE_ESCO_API_BASE", "Process")
try {
    # Packaged WebViews cannot use Vite's development proxy. This local, non-secret
    # endpoint is intentionally compiled into the desktop browser assets.
    $env:VITE_ESCO_API_BASE = "$backendBase/api"
    Invoke-ProductNative -FilePath $tauriCli.NodePath -ArgumentList $arguments -WorkingDirectory $desktop -Description "Build signing-independent local Windows desktop bundles"
}
finally {
    [Environment]::SetEnvironmentVariable("VITE_ESCO_API_BASE", $priorApiBase, "Process")
}

Write-Host "Desktop bundles are under $desktop\target\release\bundle"
Write-Host "Packaged backend endpoint: $backendBase/api"
Write-Host "Tauri CLI provenance: repository-local @tauri-apps/cli $($tauriCli.Version)"
