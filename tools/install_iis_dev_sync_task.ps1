param(
    [string]$TaskName = "BinanceBotIisDevSync"
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script = Join-Path $Root "tools\sync_iis_dev.ps1"
$PowerShellExe = Join-Path $env:windir "System32\WindowsPowerShell\v1.0\powershell.exe"

function Test-AdminAccess {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-AdminAccess)) {
    throw "Run this script elevated so it can register the IIS dev sync task."
}

$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Watch"
$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $argument -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$settings.Hidden = $true

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started '$TaskName'. IIS now recycles when workspace app files change."
