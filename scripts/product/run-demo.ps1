[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int] $FrontendPort = 4173,
    [switch] $NoBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

Write-Warning "DEMO DATA mode: synthetic fixtures only. No backend, cloud provider, or engineering analysis is started."
if ($NoBrowser) {
    & (Join-Path $PSScriptRoot "dev.ps1") -FrontendPort $FrontendPort -Demo -SkipBackend -NoBrowser
}
else {
    & (Join-Path $PSScriptRoot "dev.ps1") -FrontendPort $FrontendPort -Demo -SkipBackend
}
