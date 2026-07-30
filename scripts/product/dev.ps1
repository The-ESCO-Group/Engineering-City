$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Frontend = Join-Path $RepoRoot "frontend"
$FrontendUrl = "http://127.0.0.1:4173"
try {
  $Existing = Invoke-WebRequest -UseBasicParsing -Uri $FrontendUrl -TimeoutSec 2
  if ($Existing.StatusCode -eq 200 -and $Existing.Content -match "ESCO Engineering City") {
    Write-Host "ESCO Engineering City is already running at $FrontendUrl"
    Start-Process $FrontendUrl
    return
  }
} catch {
  # No existing frontend is listening; Vite will start below.
}
Write-Host "Starting ESCO Engineering City at $FrontendUrl"
Write-Host "No external cloud connection is launched."
Push-Location $Frontend
try { npm run dev -- --open $FrontendUrl } finally { Pop-Location }
