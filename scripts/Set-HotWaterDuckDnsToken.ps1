#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')]
    [string]$Subdomain
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$store = Join-Path $PSScriptRoot '..' 'data' 'admin'
$path = Join-Path $store 'duckdns-token'
if (-not (Get-Acl -LiteralPath $store).AreAccessRulesProtected) {
    throw 'The HotWater admin folder must have a dedicated access control list.'
}
Write-Host "HotWater certificate setup for $Subdomain.duckdns.org on $env:COMPUTERNAME."
Write-Host 'Paste the account token from the Duck DNS dashboard. The token is hidden.'
$secret = Read-Host -Prompt 'Duck DNS token' -AsSecureString
try {
    $token = [System.Net.NetworkCredential]::new('', $secret).Password.Trim()
    if ($token -cnotmatch '^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$') {
        throw 'The token must be the complete UUID shown on the Duck DNS dashboard.'
    }
    [System.IO.File]::WriteAllText($path, $token)
    Write-Host 'Token saved in the protected HotWater admin folder. You can close this window.'
} finally {
    $secret.Dispose()
    Remove-Variable token -ErrorAction SilentlyContinue
}
