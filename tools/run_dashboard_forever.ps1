param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Continue"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonExe = Join-Path $Root ".venv\Scripts\python.exe"
$RuntimeDir = Join-Path $Root "runtime"
$LogFile = Join-Path $RuntimeDir "dashboard-watchdog.log"
$StdoutLogFile = Join-Path $RuntimeDir "dashboard.stdout.log"
$StderrLogFile = Join-Path $RuntimeDir "dashboard.stderr.log"

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

if (-not (Test-Path $PythonExe)) {
    throw "Python virtual environment was not found at $PythonExe"
}

function Write-WatchdogLog([string]$Message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "$timestamp $Message"
}

function Test-PortInUse([int]$LocalPort) {
    $listener = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    return $null -ne $listener
}

Write-WatchdogLog "watchdog-started port=$Port root=$Root"

while ($true) {
    if (Test-PortInUse $Port) {
        Start-Sleep -Seconds 15
        continue
    }

    Write-WatchdogLog "dashboard-starting port=$Port"
    Push-Location $Root
    try {
        $process = Start-Process -FilePath $PythonExe -ArgumentList @("main.py", "dashboard", "--host", "127.0.0.1", "--port", "$Port") -WorkingDirectory $Root -RedirectStandardOutput $StdoutLogFile -RedirectStandardError $StderrLogFile -WindowStyle Hidden -PassThru
        $process.WaitForExit()
        Write-WatchdogLog "dashboard-exited exitCode=$($process.ExitCode)"
    } catch {
        Write-WatchdogLog "dashboard-error $($_.Exception.Message)"
    } finally {
        Pop-Location
    }

    Start-Sleep -Seconds 5
}