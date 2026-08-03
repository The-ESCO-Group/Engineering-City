[CmdletBinding()]
param(
    [switch] $SkipBackend,
    [switch] $SkipFrontend,
    [switch] $IncludeDesktop,
    [switch] $IncludeBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-ProductRepoRoot
$frontend = Join-Path $repoRoot "frontend"
$venv = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

Write-Host "ESCO Engineering City local setup"
Write-Host "Repository: $repoRoot"
Assert-NoFrontendSecrets

if (-not $SkipFrontend) {
    $nodeVersion = Assert-ProductNodeVersion
    $npm = Get-ProductCommandPath -Name "npm.cmd" -InstallHint "Install npm with Node.js."
    Write-Host "Node.js $nodeVersion"
    Invoke-ProductNative -FilePath $npm -ArgumentList @("ci") -WorkingDirectory $frontend -Description "Install locked frontend dependencies"

    if ($IncludeBrowser) {
        $npx = Get-ProductCommandPath -Name "npx.cmd" -InstallHint "Install npm with Node.js."
        Invoke-ProductNative -FilePath $npx -ArgumentList @("playwright", "install", "chromium") -WorkingDirectory $frontend -Description "Install the local Chromium E2E runtime"
    }
}

if (-not $SkipBackend) {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $python = Get-ProductCommandPath -Name "python" -InstallHint "Install Python 3.12 or newer."
        $systemVersion = (& $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')").Trim()
        if ($LASTEXITCODE -ne 0 -or $systemVersion -notmatch '^(?<major>\d+)\.(?<minor>\d+)\.') {
            throw "Unable to determine the system Python version."
        }
        if ([int]$Matches.major -lt 3 -or ([int]$Matches.major -eq 3 -and [int]$Matches.minor -lt 12)) {
            throw "Python 3.12 or newer is required; found $systemVersion."
        }
        Invoke-ProductNative -FilePath $python -ArgumentList @("-m", "venv", $venv) -WorkingDirectory $repoRoot -Description "Create repository-local Python environment"
    }
    $pythonVersion = Assert-ProductPythonVersion -PythonPath $venvPython
    Write-Host "Python $pythonVersion (.venv)"
    Invoke-ProductNative -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "--upgrade", "pip") -WorkingDirectory $repoRoot -Description "Update pip inside .venv"
    Invoke-ProductNative -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "-e", ".[dev]") -WorkingDirectory $repoRoot -Description "Install backend and test dependencies inside .venv"
}

if ($IncludeDesktop) {
    $cargo = Get-ProductCommandPath -Name "cargo" -InstallHint "Install the stable Rust toolchain."
    $rustc = Get-ProductCommandPath -Name "rustc" -InstallHint "Install the stable Rust toolchain."
    Write-Host (& $rustc --version)
    Invoke-ProductNative -FilePath $cargo -ArgumentList @("fetch", "--locked") -WorkingDirectory (Join-Path $repoRoot "desktop\src-tauri") -Description "Fetch locked desktop dependencies"
    Write-Host "Tauri CLI is validated by build-desktop.ps1 and is never installed globally by setup."
}

Write-Host "Setup complete."
Write-Host "Offline demo: .\scripts\product\run-demo.ps1"
Write-Host "Backend + frontend: .\scripts\product\dev.ps1"
