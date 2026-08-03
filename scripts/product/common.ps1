Set-StrictMode -Version Latest

function Get-ProductRepoRoot {
    [CmdletBinding()]
    param()

    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
}

function Get-ProductCommandPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Name,

        [string] $InstallHint = ""
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) {
        $suffix = if ($InstallHint) { " $InstallHint" } else { "" }
        throw "Required command '$Name' was not found.$suffix"
    }
    return $command.Source
}

function Assert-ProductNodeVersion {
    [CmdletBinding()]
    param([int] $MinimumMajor = 20)

    $node = Get-ProductCommandPath -Name "node" -InstallHint "Install Node.js $MinimumMajor or newer."
    $raw = (& $node --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $raw -notmatch '^v(?<major>\d+)\.') {
        throw "Unable to determine the installed Node.js version."
    }
    if ([int]$Matches.major -lt $MinimumMajor) {
        throw "Node.js $MinimumMajor or newer is required; found $raw."
    }
    return $raw
}

function Get-ProductLocalTauriCli {
    [CmdletBinding()]
    param()

    $repoRoot = Get-ProductRepoRoot
    $desktopRoot = Join-Path $repoRoot "desktop"
    $manifestPath = Join-Path $desktopRoot "package.json"
    $lockPath = Join-Path $desktopRoot "package-lock.json"
    $installedRoot = Join-Path $desktopRoot "node_modules\@tauri-apps\cli"
    $installedManifestPath = Join-Path $installedRoot "package.json"

    foreach ($requiredPath in @($manifestPath, $lockPath, $installedManifestPath)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Repository-local Tauri CLI files are missing. Run '.\scripts\product\setup.ps1 -IncludeDesktop' before building installers. Missing: '$requiredPath'."
        }
    }

    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $declaredVersion = [string]$manifest.devDependencies.'@tauri-apps/cli'
    if ($declaredVersion -notmatch '^2\.\d+\.\d+$') {
        throw "desktop/package.json must pin @tauri-apps/cli to one exact Tauri CLI 2 version; found '$declaredVersion'."
    }

    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json -AsHashtable -Depth 100
    $lockedPackage = $lock.packages['node_modules/@tauri-apps/cli']
    if (-not $lockedPackage -or [string]$lockedPackage.version -ne $declaredVersion -or
        [string]$lockedPackage.integrity -notmatch '^sha512-') {
        throw "desktop/package-lock.json does not lock @tauri-apps/cli $declaredVersion with SHA-512 integrity. Run npm ci only after reviewing the lockfile."
    }

    $installedManifest = Get-Content -LiteralPath $installedManifestPath -Raw | ConvertFrom-Json
    if ([string]$installedManifest.version -ne $declaredVersion) {
        throw "Installed repository-local Tauri CLI version '$($installedManifest.version)' does not match the pinned version '$declaredVersion'. Run '.\scripts\product\setup.ps1 -IncludeDesktop'."
    }

    $binRelative = [string]$installedManifest.bin.tauri
    if (-not $binRelative) {
        throw "The installed @tauri-apps/cli package does not publish the expected 'tauri' executable entry."
    }
    $packageRoot = [System.IO.Path]::GetFullPath($installedRoot)
    $cliPath = [System.IO.Path]::GetFullPath((Join-Path $packageRoot $binRelative))
    $relativeCliPath = [System.IO.Path]::GetRelativePath($packageRoot, $cliPath)
    $parentPrefix = "..$([System.IO.Path]::DirectorySeparatorChar)"
    if ([System.IO.Path]::IsPathRooted($relativeCliPath) -or
        $relativeCliPath -eq ".." -or $relativeCliPath.StartsWith($parentPrefix, [StringComparison]::Ordinal)) {
        throw "The repository-local Tauri CLI executable escaped its installed package directory."
    }
    if (-not (Test-Path -LiteralPath $cliPath -PathType Leaf)) {
        throw "The repository-local Tauri CLI executable was not found at '$cliPath'. Run '.\scripts\product\setup.ps1 -IncludeDesktop'."
    }

    return [pscustomobject]@{
        NodePath = Get-ProductCommandPath -Name "node" -InstallHint "Install Node.js 20 or newer."
        CliPath = $cliPath
        Version = $declaredVersion
        PackageRoot = $packageRoot
    }
}

function Assert-ProductPythonVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $PythonPath,

        [int] $MinimumMajor = 3,

        [int] $MinimumMinor = 12
    )

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Python executable was not found at '$PythonPath'. Run scripts/product/setup.ps1 first."
    }
    $version = (& $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')").Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^(?<major>\d+)\.(?<minor>\d+)\.') {
        throw "Unable to determine the Python version at '$PythonPath'."
    }
    if ([int]$Matches.major -lt $MinimumMajor -or
        ([int]$Matches.major -eq $MinimumMajor -and [int]$Matches.minor -lt $MinimumMinor)) {
        throw "Python $MinimumMajor.$MinimumMinor or newer is required; found $version."
    }
    return $version
}

function Invoke-ProductNative {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $FilePath,

        [string[]] $ArgumentList = @(),

        [Parameter(Mandatory)]
        [string] $WorkingDirectory,

        [string] $Description = $FilePath
    )

    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
        throw "Working directory '$WorkingDirectory' does not exist."
    }

    Write-Host "--> $Description"
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $FilePath @ArgumentList
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 0 }
        if ($exitCode -ne 0) {
            throw "$Description failed with exit code $exitCode."
        }
    }
    finally {
        Pop-Location
    }
}

function Test-ProductLoopbackUrl {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Url)

    $candidate = $null
    if (-not [Uri]::TryCreate($Url.Trim(), [UriKind]::Absolute, [ref]$candidate)) {
        return $false
    }
    if ($candidate.Scheme -ne "http" -or $candidate.UserInfo -or $candidate.Query -or $candidate.Fragment) {
        return $false
    }

    $hostText = $candidate.DnsSafeHost.Trim('[', ']').ToLowerInvariant()
    if ($hostText -eq "localhost") {
        return $true
    }

    $address = $null
    if (-not [System.Net.IPAddress]::TryParse($hostText, [ref]$address)) {
        return $false
    }
    return [System.Net.IPAddress]::IsLoopback($address)
}

function ConvertTo-ProductLoopbackUrl {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Url)

    if (-not (Test-ProductLoopbackUrl -Url $Url)) {
        throw "Only a structural http:// loopback URL (localhost, 127.0.0.0/8, or ::1) without credentials, query, or fragment is allowed: '$Url'."
    }
    $candidate = [Uri]$Url.Trim()
    $path = $candidate.AbsolutePath.TrimEnd('/')
    return "$($candidate.GetLeftPart([UriPartial]::Authority))$path"
}

function Invoke-ProductLoopbackRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Url,
        [ValidateRange(1, 30)][int] $TimeoutSeconds = 2
    )

    $normalized = ConvertTo-ProductLoopbackUrl -Url $Url
    $response = Invoke-WebRequest -UseBasicParsing -Uri $normalized -TimeoutSec $TimeoutSeconds `
        -MaximumRedirection 0 -SkipHttpErrorCheck
    $requestUri = $response.BaseResponse.RequestMessage.RequestUri
    if ($requestUri -and -not (Test-ProductLoopbackUrl -Url $requestUri.AbsoluteUri)) {
        throw "The local service response escaped the loopback boundary: '$($requestUri.AbsoluteUri)'."
    }
    if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
        throw "Loopback request returned HTTP $($response.StatusCode); redirects and non-success responses are not accepted."
    }
    return $response
}

function Test-ProductTcpPortInUse {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateRange(1, 65535)][int] $Port,
        [ValidateRange(100, 5000)][int] $TimeoutMilliseconds = 500
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync("127.0.0.1", $Port)
        return $connection.Wait($TimeoutMilliseconds) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Assert-NoFrontendSecrets {
    [CmdletBinding()]
    param()

    $unsafe = Get-ChildItem Env: | Where-Object {
        $_.Name -like "VITE_*" -and $_.Name -match '(?i)(SECRET|PASSWORD|TOKEN|API[_-]?KEY|CREDENTIAL|PRIVATE[_-]?KEY)'
    }
    if ($unsafe) {
        $names = ($unsafe.Name | Sort-Object) -join ", "
        throw "Refusing to bundle secret-looking Vite variables: $names. VITE_* values are embedded in browser assets."
    }
}

function Get-ProductLogRoot {
    [CmdletBinding()]
    param()

    $root = Join-Path (Get-ProductRepoRoot) "logs\product"
    [System.IO.Directory]::CreateDirectory($root) | Out-Null
    return $root
}

function Start-ProductLoggedProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $FilePath,
        [string[]] $ArgumentList = @(),
        [Parameter(Mandatory)][string] $WorkingDirectory,
        [Parameter(Mandatory)][string] $Name
    )

    $logRoot = Get-ProductLogRoot
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdout = Join-Path $logRoot "$Name-$timestamp.stdout.log"
    $stderr = Join-Path $logRoot "$Name-$timestamp.stderr.log"
    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -WindowStyle Hidden -PassThru

    return [pscustomobject]@{
        Name = $Name
        Process = $process
        Stdout = $stdout
        Stderr = $stderr
    }
}

function Stop-ProductProcess {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Handle)

    $process = $Handle.Process
    if (-not $process.HasExited) {
        try {
            $null = $process.CloseMainWindow()
            if (-not $process.WaitForExit(1500)) {
                $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($taskkill) {
                    & $taskkill.Source /PID $process.Id /T /F *> $null
                }
                else {
                    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                }
            }
        }
        catch {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Wait-ProductHttp {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Url,
        [ValidateRange(1, 300)][int] $TimeoutSeconds = 45,
        [string] $ServiceName = "local service"
    )

    $normalized = ConvertTo-ProductLoopbackUrl -Url $Url
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastFailure = "no response"
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        try {
            return Invoke-ProductLoopbackRequest -Url $normalized -TimeoutSeconds 2
        }
        catch {
            $lastFailure = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 400
    }
    throw "$ServiceName did not become ready at $normalized within $TimeoutSeconds seconds. Last result: $lastFailure"
}

function Write-ProductLogTail {
    [CmdletBinding()]
    param([Parameter(Mandatory)] $Handle, [int] $Lines = 30)

    foreach ($path in @($Handle.Stdout, $Handle.Stderr)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Write-Warning "Last output from $path"
            Get-Content -LiteralPath $path -Tail $Lines | Write-Host
        }
    }
}

function Assert-ProductVersion {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $Version)

    if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
        throw "Version '$Version' is not a supported semantic version (for example 1.2.3 or 1.2.3-rc.1)."
    }
}
