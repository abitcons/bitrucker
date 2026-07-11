param(
    [string]$AppPoolName = "BinanceBot",
    [switch]$Watch,
    [int]$DebounceSeconds = 2
)

$ErrorActionPreference = "Stop"

function Hide-ConsoleWindow {
    if ($env:OS -ne "Windows_NT") {
        return
    }

    try {
        Add-Type -Namespace Win32 -Name ConsoleWindow -MemberDefinition @"
            [System.Runtime.InteropServices.DllImport("kernel32.dll")]
            public static extern System.IntPtr GetConsoleWindow();

            [System.Runtime.InteropServices.DllImport("user32.dll")]
            public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
"@
        $handle = [Win32.ConsoleWindow]::GetConsoleWindow()
        if ($handle -ne [IntPtr]::Zero) {
            [Win32.ConsoleWindow]::ShowWindow($handle, 0) | Out-Null
        }
    } catch {
    }
}

Hide-ConsoleWindow

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AppCmd = Join-Path $env:windir "System32\inetsrv\appcmd.exe"
$LogFile = Join-Path $Root "runtime\iis-dev-sync.log"

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile -Parent) | Out-Null

function Write-DevSyncLog([string]$Message) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "$timestamp $Message"
}

function Invoke-IisRecycle {
    if (-not (Test-Path $AppCmd)) {
        throw "IIS appcmd.exe was not found at $AppCmd"
    }
    & $AppCmd recycle apppool "/apppool.name:$AppPoolName" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to recycle IIS app pool $AppPoolName"
    }
    Write-DevSyncLog "recycled appPool=$AppPoolName"
}

function Test-AdminAccess {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-AdminAccess)) {
    throw "Run this script elevated so it can recycle the IIS app pool."
}

Invoke-IisRecycle

if (-not $Watch) {
    exit 0
}

Write-DevSyncLog "watch-started root=$Root appPool=$AppPoolName"
$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $Root
$watcher.IncludeSubdirectories = $true
$watcher.Filter = "*.*"
$watcher.NotifyFilter = [System.IO.NotifyFilters]'FileName, LastWrite, CreationTime'
$watcher.EnableRaisingEvents = $true

$lastRecycleAt = Get-Date "2000-01-01"
$ignoredParts = @("\.git\", "\.venv\", "\runtime\", "\__pycache__\")
$extensions = @(".py", ".html", ".css", ".js", ".json", ".config", ".yml", ".yaml")

try {
    while ($true) {
        $change = $watcher.WaitForChanged([System.IO.WatcherChangeTypes]::All, 1000)
        if ($change.TimedOut) {
            continue
        }

        $fullPath = Join-Path $Root $change.Name
        $normalized = $fullPath.ToLowerInvariant()
        $ignored = $false
        foreach ($part in $ignoredParts) {
            if ($normalized.Contains($part)) {
                $ignored = $true
                break
            }
        }
        if ($ignored) {
            continue
        }

        $extension = [System.IO.Path]::GetExtension($fullPath).ToLowerInvariant()
        if ($extensions -notcontains $extension) {
            continue
        }

        $now = Get-Date
        if (($now - $lastRecycleAt).TotalSeconds -lt $DebounceSeconds) {
            continue
        }

        Start-Sleep -Seconds $DebounceSeconds
        Invoke-IisRecycle
        $lastRecycleAt = Get-Date
    }
} finally {
    $watcher.Dispose()
    Write-DevSyncLog "watch-stopped"
}
