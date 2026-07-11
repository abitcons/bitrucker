param(
    [string]$ServiceName = "cloudflared-binance-bot",
    [string]$DisplayName = "Cloudflared Binance Bot"
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SourceConfig = Join-Path $Root ".cloudflared\config.yml"
$CloudflaredExe = "C:\Program Files\cloudflared\cloudflared.exe"
$ServiceDir = Join-Path $env:ProgramData $ServiceName
$ServiceConfig = Join-Path $ServiceDir "config.yml"
$LogFile = Join-Path $ServiceDir "cloudflared.log"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Administrator)) {
    throw "Run this script from an elevated PowerShell prompt so it can install the Windows service."
}

if (-not (Test-Path $CloudflaredExe)) {
    throw "cloudflared.exe was not found at $CloudflaredExe"
}

if (-not (Test-Path $SourceConfig)) {
    throw "Tunnel config was not found at $SourceConfig"
}

New-Item -ItemType Directory -Force -Path $ServiceDir | Out-Null

$configText = Get-Content -Path $SourceConfig -Raw
if ($configText -match '(?m)^credentials-file:\s*(.+)$') {
    $sourceCredentials = $Matches[1].Trim()
    if (-not (Test-Path $sourceCredentials)) {
        throw "Tunnel credentials file was not found at $sourceCredentials"
    }
    $serviceCredentials = Join-Path $ServiceDir (Split-Path $sourceCredentials -Leaf)
    Copy-Item -Path $sourceCredentials -Destination $serviceCredentials -Force
    $escapedCredentials = $serviceCredentials -replace '\\', '\\'
    $configText = $configText -replace '(?m)^credentials-file:.*$', "credentials-file: $escapedCredentials"
}

Set-Content -Path $ServiceConfig -Value $configText -Encoding UTF8

$binaryPath = "`"$CloudflaredExe`" tunnel --config `"$ServiceConfig`" --loglevel info --logfile `"$LogFile`" run"
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe config $ServiceName binPath= $binaryPath start= auto DisplayName= $DisplayName | Out-Null
} else {
    New-Service -Name $ServiceName -BinaryPathName $binaryPath -DisplayName $DisplayName -StartupType Automatic | Out-Null
}

Start-Service -Name $ServiceName
Write-Host "Installed and started '$ServiceName' using $ServiceConfig"