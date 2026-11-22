#Requires -Version 7.0
param([Parameter(Mandatory)][ValidateSet('Admin', 'Https', 'Validate')][string]$Component)

$ErrorActionPreference = 'Stop'
$store = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$project = (Get-Item -LiteralPath (Join-Path $store '..\..')).FullName
$env:HOTWATER_ADMIN_STORE = $store
$zoneFile = Join-Path $store 'duckdns-zone'
if (Test-Path -LiteralPath $zoneFile) {
    $zone = (Get-Content -LiteralPath $zoneFile -Raw).Trim()
    if ($zone -cnotmatch '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.duckdns\.org$') {
        throw 'Invalid HotWater Duck DNS zone.'
    }
    $env:HOTWATER_DUCKDNS_ZONE = $zone
}
$log = Join-Path 'C:\HotWaterRuntime\logs' "admin-$Component.log"
"[$(Get-Date -Format o)] HotWater $Component on $env:COMPUTERNAME" | Add-Content -LiteralPath $log
if ($Component -eq 'Admin') {
    & (Join-Path $project '.runtime\python313\python.exe') -B -u (Join-Path $PSScriptRoot 'admin.py') >> $log 2>&1
} else {
    $env:XDG_DATA_HOME = Join-Path $store 'caddy-data'
    $env:XDG_CONFIG_HOME = Join-Path $store 'caddy-config'
    $env:HOTWATER_PROXY_KEY = (Get-Content -LiteralPath (Join-Path $store 'proxy.key') -Raw).Trim()
    if ($env:HOTWATER_DUCKDNS_ZONE) {
        $env:HOTWATER_DUCKDNS_TOKEN = (Get-Content -LiteralPath (Join-Path $store 'duckdns-token') -Raw).Trim()
        if ($env:HOTWATER_DUCKDNS_TOKEN -cnotmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
            throw 'Invalid HotWater Duck DNS token.'
        }
    }
    $verb = if ($Component -eq 'Validate') { 'validate' } else { 'run' }
    & (Join-Path $PSScriptRoot 'caddy.exe') $verb --config (Join-Path $PSScriptRoot 'Caddyfile.hotwater') --adapter caddyfile 2>&1 |
        ForEach-Object {
            $line = $_.ToString()
            if ($env:HOTWATER_DUCKDNS_TOKEN) {
                # The Duck DNS client can include its request URL in errors.
                $line = $line.Replace($env:HOTWATER_DUCKDNS_TOKEN, '[redacted]')
            }
            $line
        } >> $log
}
exit $LASTEXITCODE
