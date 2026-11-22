#Requires -Version 7.0
#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')]
    [string]$Subdomain,
    [Parameter(Mandatory)]
    [string]$CaddyBinary
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$project = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$store = Join-Path $project 'data' 'admin'
$runtime = Join-Path $store 'runtime'
$zone = "$Subdomain.duckdns.org"
$zoneFile = Join-Path $store 'duckdns-zone'
$routes = Join-Path $runtime 'Caddyfile.hotwater.d'
$tasks = @('HotWater-Https', 'HotWater-Admin', 'HotWater-Gauge')
$files = [ordered]@{
    'caddy.exe' = (Get-Item -LiteralPath $CaddyBinary).FullName
    'admin.py' = Join-Path $project 'admin.py'
    'Caddyfile.hotwater' = Join-Path $project 'Caddyfile.hotwater'
    'run-hotwater-admin.ps1' = Join-Path $PSScriptRoot 'run-hotwater-admin.ps1'
}

if ((Test-Path -LiteralPath $zoneFile) -or (Test-Path -LiteralPath $routes)) {
    throw 'A Duck DNS configuration is already present. Inspect the installed configuration first.'
}
if (-not (Get-Acl -LiteralPath $store).AreAccessRulesProtected) {
    throw 'The HotWater admin store must have a dedicated access control list.'
}
$null = Get-ScheduledTask -TaskName $tasks
$modules = & $files['caddy.exe'] list-modules
if ($LASTEXITCODE -ne 0 -or 'dns.providers.duckdns' -notin $modules) {
    throw 'The staged Caddy binary must include dns.providers.duckdns.'
}
if (-not $PSCmdlet.ShouldProcess($zone, 'Install and restart the HotWater gauge, admin, and HTTPS tasks')) {
    return
}

function Stop-HotWaterTasks {
    foreach ($task in $tasks) {
        if ((Get-ScheduledTask -TaskName $task).State -eq 'Running') {
            Stop-ScheduledTask -TaskName $task
        }
    }
    $deadline = (Get-Date).AddSeconds(15)
    do {
        if (-not (Get-ScheduledTask -TaskName $tasks | Where-Object State -eq Running)) { return }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw 'The HotWater web tasks did not stop.'
}

function Start-HotWaterTasks {
    foreach ($task in @('HotWater-Gauge', 'HotWater-Admin', 'HotWater-Https')) {
        Start-ScheduledTask -TaskName $task
    }
}

$backup = Join-Path $store ('duckdns-rollback-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$null = New-Item -ItemType Directory -Path $backup
Write-Output "$(Get-Date -Format o) HotWater Duck DNS on $env:COMPUTERNAME. Backup: $backup"
foreach ($name in $files.Keys) {
    Copy-Item -LiteralPath (Join-Path $runtime $name) -Destination $backup
}
$preview = Join-Path $store 'duckdns-preview'
$null = New-Item -ItemType Directory -Path (Join-Path $preview 'Caddyfile.hotwater.d') -Force
Copy-Item -LiteralPath $files['Caddyfile.hotwater'] -Destination $preview -Force
$duckDnsConfig = Join-Path $project 'Caddyfile.duckdns'
Copy-Item -LiteralPath $duckDnsConfig -Destination (Join-Path $preview 'Caddyfile.hotwater.d' 'duckdns.caddy') -Force

$env:HOTWATER_DUCKDNS_ZONE = $zone
$token = (Get-Content -LiteralPath (Join-Path $store 'duckdns-token') -Raw).Trim()
if ($token -cnotmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
    throw 'Invalid HotWater Duck DNS token.'
}
Remove-Variable token
$env:HOTWATER_DUCKDNS_TOKEN = 'validation-only'
$env:HOTWATER_PROXY_KEY = 'validation-only'
$env:XDG_DATA_HOME = Join-Path $preview 'data'
$env:XDG_CONFIG_HOME = Join-Path $preview 'config'
try {
    & $files['caddy.exe'] validate --config (Join-Path $preview 'Caddyfile.hotwater') --adapter caddyfile
    if ($LASTEXITCODE -ne 0) { throw 'Caddy could not validate the combined HotWater configuration.' }
} finally {
    $env:HOTWATER_DUCKDNS_TOKEN = $null
    $env:HOTWATER_PROXY_KEY = $null
}

try {
    Write-Output 'Restarting the HotWater web tasks with the validated configuration.'
    Stop-HotWaterTasks
    foreach ($name in $files.Keys) {
        Copy-Item -LiteralPath $files[$name] -Destination (Join-Path $runtime $name) -Force
    }
    $null = New-Item -ItemType Directory -Path $routes
    Copy-Item -LiteralPath $duckDnsConfig -Destination (Join-Path $routes 'duckdns.caddy')
    Set-Content -LiteralPath $zoneFile -Value $zone -Encoding ascii
    Start-HotWaterTasks
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $webReady = (Test-NetConnection -ComputerName '127.0.0.1' -Port 8125 -InformationLevel Quiet -WarningAction SilentlyContinue) -and
                    (Test-NetConnection -ComputerName '127.0.0.1' -Port 8126 -InformationLevel Quiet -WarningAction SilentlyContinue) -and
                    (Test-NetConnection -ComputerName '192.168.50.10' -Port 8443 -InformationLevel Quiet -WarningAction SilentlyContinue)
    } until ($webReady -or (Get-Date) -ge $deadline)
    if (-not $webReady) { throw 'A HotWater web listener did not come back after restart.' }
    Write-Output "HotWater web listeners are running. Caddy manages the certificate for *.$zone."
} catch {
    Write-Warning 'Restoring the previous HotWater web runtime.'
    Stop-HotWaterTasks
    Remove-Item -LiteralPath $zoneFile, (Join-Path $routes 'duckdns.caddy') -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $routes) { Remove-Item -LiteralPath $routes }
    foreach ($name in $files.Keys) {
        Copy-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $runtime $name) -Force
    }
    Start-HotWaterTasks
    throw
}
