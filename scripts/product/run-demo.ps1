$ErrorActionPreference = "Stop"
$env:VITE_DEMO_MODE = "true"
Write-Host "DEMO DATA mode: synthetic fixtures only; no engineering analysis will be performed."
& (Join-Path $PSScriptRoot "dev.ps1")

