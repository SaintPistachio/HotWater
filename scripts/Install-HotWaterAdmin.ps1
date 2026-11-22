#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][ValidateSet('Prepare','Activate')][string]$Stage)

$ErrorActionPreference = 'Stop'
$project = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$store = Join-Path $project 'data\admin'
$runtime = Join-Path $store 'runtime'
$certificate = Join-Path $store 'caddy-data\caddy\pki\authorities\local\root.crt'
$phoneCertificate = Join-Path $project 'data\HotWater-CA.cer'
$python = Join-Path $project '.runtime\python313\python.exe'
$pwsh = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
if (-not (Test-Path -LiteralPath $pwsh -PathType Leaf)) {
    throw 'PowerShell 7 must be installed under Program Files for the Local Service startup tasks.'
}
$tasks = @('HotWater-Admin','HotWater-Https')
$firewall = 'HotWater-Admin-Https'
$logDirectory = 'C:\HotWaterRuntime\logs'
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
$service = [Security.Principal.SecurityIdentifier]::new('S-1-5-19')
$serviceName = $service.Translate([Security.Principal.NTAccount]).Value

function Protect-AdminDirectory {
    $directory = New-Item -ItemType Directory -Path $store -Force
    $acl = [IO.FileSystemAclExtensions]::GetAccessControl($directory, [Security.AccessControl.AccessControlSections]::Access)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in $acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier])) {
        $acl.RemoveAccessRuleSpecific($rule)
    }
    foreach ($identity in @($owner, 'S-1-5-18', 'S-1-5-32-544', $service)) {
        $sid = [Security.Principal.SecurityIdentifier]$identity
        $rights = if ($sid -eq $service) { 'Modify' } else { 'FullControl' }
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid, $rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    [IO.FileSystemAclExtensions]::SetAccessControl($directory, $acl)
}

function Install-ProxyBinary {
    $version = '2.11.9'
    $digest = 'AD93112D335EE687100C4C9F11BCED16CA590377976B7FE7883882B9E797E472'
    $binary = Join-Path $runtime 'caddy.exe'
    if (Test-Path -LiteralPath $binary -PathType Leaf) { return }
    $scratch = Join-Path 'C:\HotWaterRuntime' "caddy-$version"
    $null = New-Item -ItemType Directory -Path $scratch -Force
    $name = "caddy_${version}_windows_amd64.zip"
    $archive = Join-Path $scratch $name
    if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive).Hash -ne $digest) {
        Invoke-WebRequest "https://github.com/caddyserver/caddy/releases/download/v$version/$name" -OutFile $archive
    }
    if ((Get-FileHash -LiteralPath $archive).Hash -ne $digest) { throw 'Caddy archive checksum differs from the pinned release.' }
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try { [IO.Compression.ZipFileExtensions]::ExtractToFile($zip.GetEntry('caddy.exe'), $binary, $false) }
    finally { $zip.Dispose() }
    & $binary version
    if ($LASTEXITCODE -ne 0) { throw 'Caddy release did not execute.' }
}

function Show-AdminCertificate {
    $ca = [System.Security.Cryptography.X509Certificates.X509CertificateLoader]::LoadCertificateFromFile($certificate)
    try {
        $fingerprint = $ca.GetCertHashString([Security.Cryptography.HashAlgorithmName]::SHA256)
        if (Test-Path -LiteralPath $phoneCertificate -PathType Leaf) {
            if ((Get-FileHash -LiteralPath $phoneCertificate).Hash -ne $fingerprint) {
                throw 'The existing phone certificate differs from the active CA. Review before replacing it.'
            }
        } else {
            [IO.File]::WriteAllBytes($phoneCertificate, $ca.RawData)
        }
        Write-Output "Phone CA file: $phoneCertificate"
        Write-Output "Phone CA SHA-256 fingerprint: $fingerprint"
    } finally { $ca.Dispose() }
}

if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, "$Stage HotWater admin services")) { return }
if ($Stage -eq 'Activate') {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Activation requires an elevated PowerShell 7 window for Task Scheduler and Windows Firewall.'
    }
}
Write-Output "[$(Get-Date -Format o)] $Stage HotWater admin on $env:COMPUTERNAME. Logs: $logDirectory"
if ($Stage -eq 'Prepare') {
    Protect-AdminDirectory
    $null = New-Item -ItemType Directory -Path $runtime -Force
    Install-ProxyBinary
    foreach ($file in @('admin.py','admin.html','admin-login.html','admin.js','admin.css','Caddyfile.hotwater','scripts\run-hotwater-admin.ps1')) {
        Copy-Item -LiteralPath (Join-Path $project $file) -Destination $runtime -Force
    }
    $key = Join-Path $store 'proxy.key'
    if (-not (Test-Path -LiteralPath $key)) {
        $secret = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
        Set-Content -LiteralPath $key -Encoding ascii -Value ([Convert]::ToHexString($secret).ToLowerInvariant())
    }
    & $pwsh -NoProfile -NonInteractive -File (Join-Path $runtime 'run-hotwater-admin.ps1') -Component Validate
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $certificate -PathType Leaf)) {
        throw "Caddy validation did not produce the admin root certificate. Check $logDirectory\admin-Validate.log."
    }
    Show-AdminCertificate
    Write-Output "Prepared. Set the password interactively with & '$python' -B '$(Join-Path $runtime 'admin.py')' --set-password"
    return
}

foreach ($path in @($python, (Join-Path $runtime 'caddy.exe'), (Join-Path $runtime 'run-hotwater-admin.ps1'), (Join-Path $store 'password.json'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path. Prepare the admin runtime and set the owner password first." }
}
if (Get-ScheduledTask -TaskName $tasks -ErrorAction SilentlyContinue) { throw 'An admin task already exists. Inspect the existing tasks before registering.' }
if (Get-NetFirewallRule -Name $firewall -ErrorAction SilentlyContinue) { throw 'The admin firewall rule already exists. Inspect its scope before registering.' }

$created = [Collections.Generic.List[string]]::new()
$ruleCreated = $false
try {
    foreach ($name in $tasks) {
        $component = $name.Split('-')[1]
        $action = New-ScheduledTaskAction -Execute $pwsh -Argument (
            '-NoProfile -NonInteractive -File "{0}" -Component {1}' -f (Join-Path $runtime 'run-hotwater-admin.ps1'), $component)
        $principal = New-ScheduledTaskPrincipal -UserId $serviceName -LogonType ServiceAccount -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $null = Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings -Trigger $trigger
        $created.Add($name)
        Start-ScheduledTask -TaskName $name
    }
    $deadline = (Get-Date).AddSeconds(30)
    $probe = @'
import ssl
import sys
import urllib.request

context = ssl.create_default_context(cafile=sys.argv[1])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                    urllib.request.HTTPSHandler(context=context))
try:
    with opener.open("https://192.168.50.10:8443/login", timeout=3) as response:
        print(response.status)
except OSError as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)
'@
    do {
        Start-Sleep -Milliseconds 500
        if (-not (Test-Path -LiteralPath $certificate)) { continue }
        $status = $probe | & $python -B - $certificate 2>$null
        if ($LASTEXITCODE -eq 0 -and $status -eq '200') { break }
    } while ((Get-Date) -lt $deadline)
    if ($status -ne '200') { throw 'The admin sign-in page did not answer with verified HTTPS.' }
    $null = New-NetFirewallRule -Name $firewall -DisplayName 'HotWater Admin HTTPS' -Direction Inbound `
        -Protocol TCP -LocalAddress '192.168.50.10' -LocalPort 8443 -RemoteAddress '192.168.50.0/24' -Action Allow
    $ruleCreated = $true
    Write-Output 'Admin HTTPS is online. Heater controls stay locked until verified.'
    Show-AdminCertificate
} catch {
    if ($ruleCreated) { Remove-NetFirewallRule -Name $firewall -ErrorAction SilentlyContinue }
    foreach ($name in $created) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    }
    throw
}
