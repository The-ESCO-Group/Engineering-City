[CmdletBinding()]
param(
    [string] $OutputDirectory = "",
    [ValidateRange(100, 20000)][int] $TailLines = 5000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "common.ps1")

$repoRoot = Get-ProductRepoRoot
$logRoot = Join-Path $repoRoot "logs\product"
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repoRoot "dist\support" }
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($outputRoot) | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$staging = Join-Path $outputRoot "esco-support-$stamp"
$zipPath = "$staging.zip"
if ((Test-Path -LiteralPath $staging) -or (Test-Path -LiteralPath $zipPath)) {
    throw "Support bundle target already exists; refusing to overwrite it."
}
[System.IO.Directory]::CreateDirectory($staging) | Out-Null

$redactions = @(
    @{ Pattern = '(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*).+$'; Replacement = '${1}[REDACTED]' },
    @{ Pattern = '(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+'; Replacement = '${1}[REDACTED]' },
    @{ Pattern = '(?i)((?:api[_-]?key|password|secret|token|credential|private[_-]?key)\s*[:=]\s*)[^\s,;]+'; Replacement = '${1}[REDACTED]' },
    @{ Pattern = '(?is)-----BEGIN [^-]*(?:PRIVATE KEY)-----.*?-----END [^-]*(?:PRIVATE KEY)-----'; Replacement = '[REDACTED PRIVATE KEY]' },
    @{ Pattern = '(?i)\b[A-Z]:\\Users\\[^\\\s]+'; Replacement = '[USER_PROFILE]' }
)

$logSources = @([pscustomobject]@{ Prefix = "service"; Path = $logRoot })
if ($env:LOCALAPPDATA) {
    $nativeLogRoot = Join-Path $env:LOCALAPPDATA "com.escogroup.engineeringcity\logs"
    $logSources += [pscustomobject]@{ Prefix = "desktop"; Path = $nativeLogRoot }
}
foreach ($source in $logSources) {
    if (Test-Path -LiteralPath $source.Path -PathType Container) {
      foreach ($log in Get-ChildItem -LiteralPath $source.Path -Filter "*.log" -File) {
        $content = (Get-Content -LiteralPath $log.FullName -Tail $TailLines) -join [Environment]::NewLine
        foreach ($redaction in $redactions) {
            $content = [regex]::Replace($content, $redaction.Pattern, $redaction.Replacement)
        }
        Set-Content -LiteralPath (Join-Path $staging "$($source.Prefix)-$($log.Name)") -Value $content -Encoding utf8
      }
    }
}

@"
This bundle includes only tailed service and native desktop .log files plus tool versions.
Redaction is best-effort. Review every file before sharing it outside the authorized
engineering team; project identifiers or operational context may still be sensitive.
"@ | Set-Content -LiteralPath (Join-Path $staging "REVIEW-BEFORE-SHARING.txt") -Encoding utf8

function Get-SupportToolVersion {
    param([Parameter(Mandatory)][scriptblock] $Action)
    try {
        $value = & $Action
        if ($LASTEXITCODE -ne 0) { return "unavailable" }
        return ($value | Out-String).Trim()
    }
    catch {
        return "unavailable"
    }
}

$versions = [ordered]@{
    collected_utc = [DateTimeOffset]::UtcNow.ToString("o")
    os = [Environment]::OSVersion.VersionString
    powershell = $PSVersionTable.PSVersion.ToString()
    node = Get-SupportToolVersion { node --version }
    npm = Get-SupportToolVersion { npm.cmd --version }
    python = Get-SupportToolVersion { python --version }
    rustc = Get-SupportToolVersion { rustc --version }
    note = "Environment values, .env files, databases, project artifacts, and uploaded engineering files are intentionally excluded."
}
$versions | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $staging "diagnostics.json") -Encoding utf8

Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zipPath -CompressionLevel Optimal
Write-Host "Redacted support bundle: $zipPath"
Write-Host "SHA-256: $((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant())"
