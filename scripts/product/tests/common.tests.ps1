$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot "..\common.ps1")

$failures = [System.Collections.Generic.List[string]]::new()

function Assert-True {
    param([bool] $Condition, [string] $Message)
    if (-not $Condition) { $failures.Add($Message) }
}

function Assert-Throws {
    param([scriptblock] $Action, [string] $Message)
    try {
        & $Action
        $failures.Add($Message)
    }
    catch {
        # Expected.
    }
}

foreach ($url in @(
    "http://127.0.0.1:8000",
    "http://127.0.0.42:8000/api",
    "http://localhost:4173",
    "http://[::1]:8000/api/health"
)) {
    Assert-True (Test-ProductLoopbackUrl -Url $url) "Expected loopback URL to pass: $url"
}

foreach ($url in @(
    "https://127.0.0.1:8000",
    "http://localhost.example.com:8000",
    "http://127.0.0.1.example.com:8000",
    "http://user@localhost:8000",
    "http://localhost:8000?next=https://example.com",
    "http://localhost:8000/#fragment",
    "not-a-url"
)) {
    Assert-True (-not (Test-ProductLoopbackUrl -Url $url)) "Expected unsafe URL to fail: $url"
}

Assert-True `
    ((ConvertTo-ProductLoopbackUrl -Url "http://localhost:8000/api/") -eq "http://localhost:8000/api") `
    "Loopback URL normalization should remove only the trailing slash."

Assert-Throws { Assert-ProductVersion -Version "1.2" } "Invalid package version should be rejected."
Assert-ProductVersion -Version "1.2.3-rc.1"
Assert-True (-not (Test-ProductTcpPortInUse -Port 9 -TimeoutMilliseconds 100)) "A normally closed loopback port should not be reported as in use."

$secretName = "VITE_TEST_API_KEY"
$priorSecret = [Environment]::GetEnvironmentVariable($secretName, "Process")
try {
    [Environment]::SetEnvironmentVariable($secretName, "synthetic-test-only", "Process")
    Assert-Throws { Assert-NoFrontendSecrets } "Secret-looking Vite variable should be rejected."
}
finally {
    [Environment]::SetEnvironmentVariable($secretName, $priorSecret, "Process")
}

$repoRoot = Get-ProductRepoRoot
$scripts = Get-ChildItem -LiteralPath (Join-Path $repoRoot "scripts\product") -Filter "*.ps1" -Recurse -File
foreach ($script in $scripts) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($script.FullName, [ref]$tokens, [ref]$errors)
    Assert-True ($errors.Count -eq 0) "PowerShell parser errors in $($script.FullName): $($errors -join '; ')"
}

$capability = Get-Content -LiteralPath (Join-Path $repoRoot "desktop\src-tauri\capabilities\default.json") -Raw | ConvertFrom-Json
Assert-True ($capability.permissions -notcontains "opener:allow-open-path") "Desktop must not expose the unrestricted path opener."
Assert-True ($capability.permissions -notcontains "shell:allow-execute") "Desktop must not expose unrestricted shell execution."
Assert-True ($capability.permissions -notcontains "notification:default") "Frontend must use the approval/failure-only notification command."

$tauriConfig = Get-Content -LiteralPath (Join-Path $repoRoot "desktop\src-tauri\tauri.conf.json") -Raw | ConvertFrom-Json
Assert-True ($tauriConfig.app.security.csp -match "object-src 'none'") "Desktop CSP must disable object embedding."
Assert-True ($tauriConfig.app.security.csp -notmatch 'https://\*') "Desktop CSP must not allow wildcard HTTPS connections."
Assert-True ($tauriConfig.build.beforeBuildCommand -eq "npm --prefix ../frontend run build") "Tauri build hook must resolve frontend from the desktop application root."
Assert-True ($tauriConfig.build.beforeDevCommand -eq "npm --prefix ../frontend run dev") "Tauri development hook must resolve frontend from the desktop application root."
Assert-True ($tauriConfig.build.frontendDist -eq "../../frontend/dist") "Tauri frontendDist must remain relative to src-tauri/tauri.conf.json."
$windowsIcon = $tauriConfig.bundle.icon | Where-Object { $_ -match '\.ico$' } | Select-Object -First 1
Assert-True ([bool]$windowsIcon) "Tauri bundle configuration must declare a Windows ICO asset."
if ($windowsIcon) {
    Assert-True (Test-Path -LiteralPath (Join-Path $repoRoot "desktop\src-tauri\$windowsIcon") -PathType Leaf) "Declared Tauri Windows ICO asset must exist."
}
Assert-True ((Test-Path -LiteralPath (Join-Path $repoRoot "scripts\product\package-release.ps1"))) "Desktop release packaging workflow is required."

$desktopManifest = Get-Content -LiteralPath (Join-Path $repoRoot "desktop\package.json") -Raw | ConvertFrom-Json
$desktopLock = Get-Content -LiteralPath (Join-Path $repoRoot "desktop\package-lock.json") -Raw | ConvertFrom-Json -AsHashtable -Depth 100
$pinnedTauriVersion = [string]$desktopManifest.devDependencies.'@tauri-apps/cli'
$lockedTauri = $desktopLock.packages['node_modules/@tauri-apps/cli']
Assert-True ($pinnedTauriVersion -match '^2\.\d+\.\d+$') "Tauri CLI must be pinned to one exact major-version-2 release."
Assert-True ([string]$lockedTauri.version -eq $pinnedTauriVersion) "Tauri CLI lock version must match desktop/package.json."
Assert-True ([string]$lockedTauri.integrity -match '^sha512-') "Tauri CLI lock entry must include SHA-512 integrity."
if (Test-Path -LiteralPath (Join-Path $repoRoot "desktop\node_modules\@tauri-apps\cli\package.json") -PathType Leaf) {
    try {
        $localTauri = Get-ProductLocalTauriCli
        $desktopRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "desktop"))
        $relativeTauri = [System.IO.Path]::GetRelativePath($desktopRoot, $localTauri.CliPath)
        Assert-True (-not $relativeTauri.StartsWith("..")) "Tauri CLI executable must resolve beneath the repository desktop directory."
        Assert-True ($localTauri.Version -eq $pinnedTauriVersion) "Resolved local Tauri CLI version must match the manifest pin."
    }
    catch {
        $failures.Add("Repository-local Tauri CLI resolution failed: $($_.Exception.Message)")
    }
}

$desktopBuildScript = Get-Content -LiteralPath (Join-Path $repoRoot "scripts\product\build-desktop.ps1") -Raw
Assert-True ($desktopBuildScript -notmatch '(?m)\bcargo\s+tauri\b') "Desktop build must not fall back to a global cargo-tauri command."
Assert-True ($desktopBuildScript -match 'Get-ProductLocalTauriCli') "Desktop build must resolve the pinned repository-local Tauri CLI."

$developmentScript = Get-Content -LiteralPath (Join-Path $repoRoot "scripts\product\dev.ps1") -Raw
Assert-True ($developmentScript -match '"--app-dir",\s*"backend"') "Development startup must expose backend/app to Uvicorn from the repository root."

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Host "Product PowerShell tests passed ($($scripts.Count) scripts parsed)."
exit 0
