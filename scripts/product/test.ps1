$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location (Join-Path $RepoRoot "frontend")
try { npm run typecheck; npm test; npm run build } finally { Pop-Location }

