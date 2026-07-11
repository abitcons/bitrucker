param(
    [string]$EnvFile = ".env.cloudflare.bitrucker.com.local",
    [string]$TunnelId = "d65cbf3f-f422-430c-bbea-11c8ba0a13bb"
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvPath = Join-Path $Root $EnvFile
if (-not (Test-Path $EnvPath)) {
    throw "Cloudflare env file was not found at $EnvPath"
}

$values = @{}
foreach ($line in Get-Content -Path $EnvPath) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#") -or $trimmed -notmatch "=") {
        continue
    }
    $key, $value = $trimmed.Split("=", 2)
    $values[$key.Trim()] = $value.Trim()
}

$token = $values["CLOUDFLARE_API_TOKEN"]
$zoneId = $values["CLOUDFLARE_ZONE_ID"]
$domain = $values["CLOUDFLARE_DOMAIN"]

if (-not $token) { throw "CLOUDFLARE_API_TOKEN is missing from $EnvPath" }
if (-not $zoneId) { throw "CLOUDFLARE_ZONE_ID is missing from $EnvPath" }
if (-not $domain) { throw "CLOUDFLARE_DOMAIN is missing from $EnvPath" }

$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }
$apiBase = "https://api.cloudflare.com/client/v4/zones/$zoneId/dns_records"
$target = "$TunnelId.cfargotunnel.com"
$desiredNames = @($domain, "www.$domain")

function Invoke-CloudflareApi([string]$Method, [string]$Uri, [object]$Body = $null) {
    $parameters = @{ Method = $Method; Uri = $Uri; Headers = $headers; TimeoutSec = 30 }
    if ($null -ne $Body) {
        $parameters.Body = ($Body | ConvertTo-Json -Depth 10)
    }
    $response = Invoke-RestMethod @parameters
    if (-not $response.success) {
        $messages = ($response.errors | ForEach-Object { $_.message }) -join "; "
        throw "Cloudflare API request failed: $messages"
    }
    return $response
}

foreach ($name in $desiredNames) {
    $encodedName = [uri]::EscapeDataString($name)
    $existing = Invoke-CloudflareApi "GET" "$apiBase`?name=$encodedName&per_page=100"
    $cnameRecord = $null
    $conflictingWebTypes = @{ A = $true; AAAA = $true; CNAME = $true }

    foreach ($record in $existing.result) {
        if ($record.type -eq "CNAME") {
            $cnameRecord = $record
            continue
        }
        if ($conflictingWebTypes.ContainsKey([string]$record.type)) {
            Invoke-CloudflareApi "DELETE" "$apiBase/$($record.id)" | Out-Null
            Write-Host "Deleted conflicting $($record.type) record for $name"
        }
    }

    $body = @{
        type = "CNAME"
        name = $name
        content = $target
        proxied = $true
        ttl = 1
        comment = "Routes Binance bot dashboard through Cloudflare Tunnel"
    }

    if ($cnameRecord) {
        Invoke-CloudflareApi "PUT" "$apiBase/$($cnameRecord.id)" $body | Out-Null
        Write-Host "Updated CNAME $name -> $target"
    } else {
        Invoke-CloudflareApi "POST" $apiBase $body | Out-Null
        Write-Host "Created CNAME $name -> $target"
    }
}

Write-Host "bitrucker.com DNS now points at tunnel $TunnelId"