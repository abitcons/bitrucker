param(
    [string]$TaskName = "BinanceBotDashboardWatchdog",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WatchdogScript = Join-Path $Root "tools\run_dashboard_forever.ps1"
$PowerShellExe = Join-Path $env:windir "System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path $WatchdogScript)) {
    throw "Watchdog script was not found at $WatchdogScript"
}

$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$WatchdogScript`" -Port $Port"
$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $argument -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started scheduled task '$TaskName' for http://127.0.0.1:$Port/"