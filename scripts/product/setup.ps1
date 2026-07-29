$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Frontend = Join-Path $RepoRoot "frontend"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js 20 or newer is required." }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm is required." }
$NodeMajor = [int]((node --version).TrimStart("v").Split(".")[0])
if ($NodeMajor -lt 20) { throw "Node.js 20 or newer is required; found $(node --version)." }
Write-Host "Installing frontend dependencies..."
Push-Location $Frontend
try { npm install } finally { Pop-Location }
Write-Host "Setup complete. Run .\scripts\product\run-demo.ps1"

