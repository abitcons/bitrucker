param(
    [string]$SiteName = "BinanceBot",
    [string]$AppPoolName = "BinanceBot",
    [int]$Port = 8000,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root "runtime"
$VenvDir = Join-Path $Root ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$WfastcgiPy = Join-Path $VenvDir "Lib\site-packages\wfastcgi.py"
$Requirements = Join-Path $Root "requirements.txt"
$WebConfig = Join-Path $Root "web.config"
$AppCmd = Join-Path $env:windir "System32\inetsrv\appcmd.exe"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Escape-XmlAttribute([string]$Value) {
    return [System.Security.SecurityElement]::Escape($Value)
}

function Invoke-NativeCommand([string]$FilePath, [string[]]$Arguments) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Ensure-VirtualEnvironment {
    if (-not (Test-Path $PythonExe)) {
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            & $pyLauncher.Source -3 -m venv $VenvDir
        } else {
            $python = Get-Command python -ErrorAction SilentlyContinue
            if (-not $python) {
                throw "Python was not found. Install Python 3, then rerun this script."
            }
            & $python.Source -m venv $VenvDir
        }
    }

    Invoke-NativeCommand $PythonExe @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-NativeCommand $PythonExe @("-m", "pip", "install", "-r", $Requirements)

    if (-not (Test-Path $WfastcgiPy)) {
        throw "wfastcgi.py was not installed at $WfastcgiPy"
    }
}

function Write-WebConfig {
    # web.config is already checked in with Waitress reverse proxy rules (URL Rewrite to port 8050)

function Ensure-IisFeatures {
    $features = @(
        "IIS-WebServerRole",
        "IIS-WebServer",
        "IIS-CommonHttpFeatures",
        "IIS-DefaultDocument",
        "IIS-StaticContent",
        "IIS-CGI",
        "IIS-ApplicationInit"
    )

    foreach ($feature in $features) {
        $state = (Get-WindowsOptionalFeature -Online -FeatureName $feature).State
        if ($state -ne "Enabled") {
            Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart | Out-Null
        }
    }
}

function Stop-ExistingDashboardProcesses {
    Stop-ScheduledTask -TaskName "BinanceBotDashboardWatchdog" -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName "BinanceBotDashboardWatchdog" -ErrorAction SilentlyContinue | Out-Null
    $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        if (-not $commandLine) {
            continue
        }
        $isThisApp = $commandLine -like "*$Root*" -or $commandLine -like "*binance_bot*"
        $isDashboard = $commandLine -like "* dashboard *" -or $commandLine -like "*launch_with_tunnel.py*"
        if ($isThisApp -and $isDashboard) {
            Stop-Process -Id $process.ProcessId -Force
        }
    }
}

function Ensure-FastCgiRegistration {
    & $AppCmd @(
        "set",
        "config",
        "-section:system.webServer/fastCgi",
        "/-[fullPath='$PythonExe',arguments='$WfastcgiPy']",
        "/commit:apphost"
    ) 2>$null | Out-Null

    Invoke-NativeCommand $AppCmd @(
        "set",
        "config",
        "-section:system.webServer/fastCgi",
        "/+[fullPath='$PythonExe',arguments='$WfastcgiPy',maxInstances='1',idleTimeout='604800',activityTimeout='600',requestTimeout='600']",
        "/commit:apphost"
    )
}

function Ensure-AppPool {
    Import-Module WebAdministration

    if (-not (Test-Path "IIS:\AppPools\$AppPoolName")) {
        New-WebAppPool -Name $AppPoolName | Out-Null
    }

    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name managedRuntimeVersion -Value ""
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name startMode -Value "AlwaysRunning"
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name processModel.idleTimeout -Value ([TimeSpan]::Zero)
    Set-ItemProperty "IIS:\AppPools\$AppPoolName" -Name recycling.periodicRestart.time -Value ([TimeSpan]::Zero)
}

function Ensure-Website {
    Import-Module WebAdministration

    $portToken = ":${Port}:"
    $conflictingSite = Get-Website | Where-Object {
        $_.Name -ne $SiteName -and ($_.bindings.Collection | Where-Object {
            $_.protocol -eq "http" -and $_.bindingInformation.Contains($portToken)
        })
    } | Select-Object -First 1

    if ($conflictingSite) {
        throw "Port $Port is already bound by IIS site '$($conflictingSite.Name)'. Choose a different port or remove that binding."
    }

    $site = Get-Website -Name $SiteName -ErrorAction SilentlyContinue
    if (-not $site) {
        New-Website -Name $SiteName -Port $Port -IPAddress "*" -PhysicalPath $Root -ApplicationPool $AppPoolName | Out-Null
    } else {
        Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $Root
        Set-ItemProperty "IIS:\Sites\$SiteName" -Name applicationPool -Value $AppPoolName

        $hasPortBinding = $site.bindings.Collection | Where-Object {
            $_.protocol -eq "http" -and $_.bindingInformation.Contains($portToken)
        }
        if (-not $hasPortBinding) {
            New-WebBinding -Name $SiteName -Protocol "http" -IPAddress "*" -Port $Port | Out-Null
        }
    }

    Set-ItemProperty "IIS:\Sites\$SiteName" -Name serverAutoStart -Value $true
    Invoke-NativeCommand $AppCmd @("set", "app", "$SiteName/", "/preloadEnabled:true")
}

function Ensure-Permissions {
    $identity = "IIS AppPool\$AppPoolName"
    Invoke-NativeCommand "icacls.exe" @($Root, "/grant", "${identity}:(OI)(CI)(RX)", "/T")
    Invoke-NativeCommand "icacls.exe" @($RuntimeDir, "/grant", "${identity}:(OI)(CI)(M)", "/T")
}

function Ensure-WaitressService {
    $waitressScript = Join-Path $Root "run_waitress.py"
    $waitressBinary = "`"$PythonExe`" `"$waitressScript`" 8050"

    $existing = Get-Service -Name "BinanceBotWaitress" -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-Service -Name "BinanceBotWaitress" -Force -ErrorAction SilentlyContinue
        sc.exe config "BinanceBotWaitress" binPath= $waitressBinary start= auto DisplayName= "Binance Bot Waitress" | Out-Null
    } else {
        New-Service -Name "BinanceBotWaitress" -BinaryPathName $waitressBinary -DisplayName "Binance Bot Waitress" -StartupType Automatic | Out-Null
    }
    Start-Service -Name "BinanceBotWaitress"
    Start-Sleep -Seconds 3
}

function Start-And-WarmUp {
    Ensure-WaitressService
    Set-Service W3SVC -StartupType Automatic
    Start-Service W3SVC
    Start-WebAppPool -Name $AppPoolName
    Start-Website -Name $SiteName

    $uri = "http://127.0.0.1:$Port/"
    $response = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 60
    Write-Host "Warm-up status: $($response.StatusCode) $($response.StatusDescription)"
    Write-Host "IIS deployment is running at $uri"
}

if ($CheckOnly) {
    Write-Host "Root: $Root"
    Write-Host "Site: $SiteName"
    Write-Host "App pool: $AppPoolName"
    Write-Host "Port: $Port"
    Write-Host "Admin: $(Test-Administrator)"
    Write-Host "IIS appcmd: $(Test-Path $AppCmd)"
    Write-Host "Python: $(Test-Path $PythonExe) $PythonExe"
    Write-Host "wfastcgi: $(Test-Path $WfastcgiPy) $WfastcgiPy"
    Write-Host "web.config: $(Test-Path $WebConfig) $WebConfig"
    exit 0
}

if (-not (Test-Administrator)) {
    throw "Run this script from an elevated PowerShell prompt so it can configure IIS."
}

if (-not (Test-Path $AppCmd)) {
    throw "IIS appcmd.exe was not found at $AppCmd. Install IIS before rerunning this script."
}

Ensure-VirtualEnvironment
Write-WebConfig
Ensure-IisFeatures
Stop-ExistingDashboardProcesses
Ensure-FastCgiRegistration
Ensure-AppPool
Ensure-Website
Ensure-Permissions
Start-And-WarmUp