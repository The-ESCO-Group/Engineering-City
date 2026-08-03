[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int] $FrontendPort = 4173,
    [ValidateRange(1024, 65535)][int] $BackendPort = 8000,
    [switch] $SkipBackend,
    [switch] $Demo,
    [switch] $NoBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-ProductRepoRoot
$frontend = Join-Path $repoRoot "frontend"
$frontendUrl = ConvertTo-ProductLoopbackUrl -Url "http://127.0.0.1:$FrontendPort"
$launchUrl = if ($Demo) { "$frontendUrl/?demo=1" } else { $frontendUrl }
$backendBase = ConvertTo-ProductLoopbackUrl -Url "http://127.0.0.1:$BackendPort"
$backendHealth = "$backendBase/api/health"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

Assert-NoFrontendSecrets
$null = Assert-ProductNodeVersion
$npm = Get-ProductCommandPath -Name "npm.cmd" -InstallHint "Install npm with Node.js."

if (Test-ProductTcpPortInUse -Port $FrontendPort) {
    throw "Frontend port $FrontendPort is already in use. Stop the existing process or choose another -FrontendPort; an unknown demo/live runtime is never reused."
}
if (-not $SkipBackend -and (Test-ProductTcpPortInUse -Port $BackendPort)) {
    throw "Backend port $BackendPort is already in use. Stop the existing process or choose another -BackendPort so this script can supervise the complete local runtime."
}

$priorDemo = [Environment]::GetEnvironmentVariable("VITE_DEMO_MODE", "Process")
$priorApiBase = [Environment]::GetEnvironmentVariable("VITE_ESCO_API_BASE", "Process")
$priorEnvironment = [Environment]::GetEnvironmentVariable("ESCO_ENVIRONMENT", "Process")
$priorCors = [Environment]::GetEnvironmentVariable("ESCO_CORS_ORIGINS", "Process")
$priorDataDir = [Environment]::GetEnvironmentVariable("ESCO_DATA_DIR", "Process")
$priorProjectsDir = [Environment]::GetEnvironmentVariable("ESCO_PROJECTS_DIR", "Process")
$backendHandle = $null
$frontendHandle = $null

try {
    $env:VITE_DEMO_MODE = if ($Demo) { "true" } else { "false" }
    $env:VITE_ESCO_API_BASE = "$backendBase/api"
    $env:ESCO_ENVIRONMENT = if ($Demo) { "demo" } else { "development" }
    $env:ESCO_CORS_ORIGINS = "$frontendUrl,http://localhost:$FrontendPort,http://127.0.0.1:1420,http://localhost:1420,http://tauri.localhost,https://tauri.localhost,tauri://localhost"
    $env:ESCO_DATA_DIR = Join-Path $repoRoot "data"
    $env:ESCO_PROJECTS_DIR = Join-Path $repoRoot "data\projects"

    if (-not $SkipBackend) {
        $null = Assert-ProductPythonVersion -PythonPath $venvPython
        Write-Host "Starting the local backend first at $backendBase"
        $backendHandle = Start-ProductLoggedProcess -FilePath $venvPython `
            -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", [string]$BackendPort) `
            -WorkingDirectory $repoRoot -Name "backend"
        try {
            $health = Wait-ProductHttp -Url $backendHealth -TimeoutSeconds 60 -ServiceName "ESCO backend"
            $healthBody = $health.Content | ConvertFrom-Json
            if ($healthBody.status -notin @("ok", "degraded")) {
                throw "Backend returned an unexpected health status '$($healthBody.status)'."
            }
            Write-Host "Backend health: $($healthBody.status); contract $($healthBody.contract_version)"
        }
        catch {
            Write-ProductLogTail -Handle $backendHandle
            throw
        }
    }
    else {
        Write-Host "Backend startup skipped. The UI will show an offline state unless DEMO DATA mode is active."
    }

    Write-Host "Starting the frontend at $frontendUrl"
    $frontendHandle = Start-ProductLoggedProcess -FilePath $npm `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", [string]$FrontendPort, "--strictPort") `
        -WorkingDirectory $frontend -Name "frontend"
    try {
        $null = Wait-ProductHttp -Url $frontendUrl -TimeoutSeconds 45 -ServiceName "ESCO frontend"
    }
    catch {
        Write-ProductLogTail -Handle $frontendHandle
        throw
    }

    Write-Host "ESCO Engineering City is ready: $launchUrl"
    Write-Host "Support logs: $(Get-ProductLogRoot)"
    Write-Host "No external provider is enabled or contacted by this script. Press Ctrl+C to stop both services."
    if ($Demo) { Write-Warning "DEMO DATA mode is active. All visible engineering records are synthetic." }
    if (-not $NoBrowser) { Start-Process -FilePath $launchUrl }

    while ($true) {
        Start-Sleep -Seconds 1
        if ($frontendHandle.Process.HasExited) {
            Write-ProductLogTail -Handle $frontendHandle
            throw "Frontend exited unexpectedly with code $($frontendHandle.Process.ExitCode)."
        }
        if ($backendHandle -and $backendHandle.Process.HasExited) {
            Write-ProductLogTail -Handle $backendHandle
            throw "Backend exited unexpectedly with code $($backendHandle.Process.ExitCode)."
        }
    }
}
finally {
    if ($frontendHandle) { Stop-ProductProcess -Handle $frontendHandle }
    if ($backendHandle) { Stop-ProductProcess -Handle $backendHandle }
    [Environment]::SetEnvironmentVariable("VITE_DEMO_MODE", $priorDemo, "Process")
    [Environment]::SetEnvironmentVariable("VITE_ESCO_API_BASE", $priorApiBase, "Process")
    [Environment]::SetEnvironmentVariable("ESCO_ENVIRONMENT", $priorEnvironment, "Process")
    [Environment]::SetEnvironmentVariable("ESCO_CORS_ORIGINS", $priorCors, "Process")
    [Environment]::SetEnvironmentVariable("ESCO_DATA_DIR", $priorDataDir, "Process")
    [Environment]::SetEnvironmentVariable("ESCO_PROJECTS_DIR", $priorProjectsDir, "Process")
}
