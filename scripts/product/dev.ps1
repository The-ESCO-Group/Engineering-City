$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Frontend = Join-Path $RepoRoot "frontend"
Write-Host "Starting ESCO Engineering City at http://127.0.0.1:4173"
Write-Host "No external cloud connection is launched."
Push-Location $Frontend
try { npm run dev } finally { Pop-Location }

