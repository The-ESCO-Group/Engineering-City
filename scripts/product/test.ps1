[CmdletBinding()]
param(
    [switch] $IncludeE2E,
    [switch] $IncludeLiveE2E,
    [switch] $SkipBackend,
    [switch] $SkipDesktop
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-ProductRepoRoot
$frontend = Join-Path $repoRoot "frontend"
$desktop = Join-Path $repoRoot "desktop\src-tauri"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pwsh = Get-ProductCommandPath -Name "pwsh" -InstallHint "Install PowerShell 7."
$npm = Get-ProductCommandPath -Name "npm.cmd" -InstallHint "Install npm with Node.js."

Assert-NoFrontendSecrets
$null = Assert-ProductNodeVersion

Invoke-ProductNative -FilePath $pwsh -ArgumentList @("-NoLogo", "-NoProfile", "-File", (Join-Path $PSScriptRoot "tests\common.tests.ps1")) -WorkingDirectory $repoRoot -Description "Validate product PowerShell workflows"
Invoke-ProductNative -FilePath $npm -ArgumentList @("run", "lint") -WorkingDirectory $frontend -Description "Lint frontend"
Invoke-ProductNative -FilePath $npm -ArgumentList @("run", "typecheck") -WorkingDirectory $frontend -Description "Type-check frontend"
Invoke-ProductNative -FilePath $npm -ArgumentList @("test") -WorkingDirectory $frontend -Description "Run frontend unit and component tests"
Invoke-ProductNative -FilePath $npm -ArgumentList @("run", "contracts:check") -WorkingDirectory $frontend -Description "Verify generated contract consumers"
Invoke-ProductNative -FilePath $npm -ArgumentList @("run", "build") -WorkingDirectory $frontend -Description "Build frontend"
Invoke-ProductNative -FilePath $npm -ArgumentList @("audit", "--omit=dev", "--audit-level=moderate") -WorkingDirectory $frontend -Description "Audit production frontend dependencies"

if (-not $SkipBackend) {
    $null = Assert-ProductPythonVersion -PythonPath $venvPython
    Invoke-ProductNative -FilePath $venvPython -ArgumentList @("-m", "pytest") -WorkingDirectory $repoRoot -Description "Run backend tests"
    Invoke-ProductNative -FilePath $venvPython -ArgumentList @("-m", "pytest", "backend/tests_delivery", "-q") -WorkingDirectory $repoRoot -Description "Run product delivery and report tests"
}

if (-not $SkipDesktop) {
    $cargo = Get-ProductCommandPath -Name "cargo" -InstallHint "Install the stable Rust toolchain."
    Invoke-ProductNative -FilePath $cargo -ArgumentList @("fmt", "--", "--check") -WorkingDirectory $desktop -Description "Check desktop Rust formatting"
    Invoke-ProductNative -FilePath $cargo -ArgumentList @("check", "--locked") -WorkingDirectory $desktop -Description "Check desktop Rust shell"
    Invoke-ProductNative -FilePath $cargo -ArgumentList @("test", "--locked") -WorkingDirectory $desktop -Description "Run desktop Rust tests"
    Invoke-ProductNative -FilePath $cargo -ArgumentList @("clippy", "--locked", "--all-targets", "--", "-D", "warnings") -WorkingDirectory $desktop -Description "Lint desktop Rust shell with warnings denied"
}

if ($IncludeE2E) {
    Invoke-ProductNative -FilePath $npm -ArgumentList @("run", "test:e2e") -WorkingDirectory $frontend -Description "Run local browser E2E tests"
}

if ($IncludeLiveE2E) {
    if ($SkipBackend) {
        throw "-IncludeLiveE2E cannot be combined with -SkipBackend because the live acceptance test starts an isolated real backend."
    }

    Invoke-ProductNative -FilePath $npm -ArgumentList @("run", "test:e2e:live") -WorkingDirectory $frontend -Description "Run isolated real-backend browser acceptance test"
}

Write-Host "Product validation completed successfully."
