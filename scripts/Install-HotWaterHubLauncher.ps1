#Requires -Version 7.0
#Requires -RunAsAdministrator
[CmdletBinding(SupportsShouldProcess)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$taskName = 'HotWater-HubLauncher'
$runtime = 'C:\HotWaterRuntime\hub-launcher'
$state = Join-Path $runtime 'matter-state'
$logDirectory = 'C:\HotWaterRuntime\logs'
$logFile = Join-Path $logDirectory 'hub-launcher.log'
$script = Join-Path $PSScriptRoot 'hotwater-hub-launcher.cjs'
$node = (Get-Command node.exe -ErrorAction Stop).Source
$serviceSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-19')
$serviceAccount = $serviceSid.Translate([Security.Principal.NTAccount]).Value
$ownerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'

foreach ($inputPath in @($script, (Join-Path $runtime 'node_modules\@matter\main\package.json'),
        (Join-Path $runtime 'cast-packages\catt\cli.py'))) {
    if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) { throw "Missing launcher input: $inputPath" }
}

Write-Information "HotWater Hub launcher setup on $env:COMPUTERNAME. Runtime log: $logFile" -InformationAction Continue
if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, "Register and start $taskName for $serviceAccount")) { return }
if (Get-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "$taskName already exists. Use Task Scheduler to start the existing instance."
}

$null = New-Item -ItemType Directory -Path $state, $logDirectory -Force
& $icacls $state /inheritance:r /grant:r "*${ownerSid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-19:(OI)(CI)M'
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the Matter state directory.' }

foreach ($directory in @((Join-Path $runtime 'node_modules'), (Join-Path $runtime 'cast-packages'))) {
    & $icacls $directory /grant '*S-1-5-19:(OI)(CI)RX'
    if ($LASTEXITCODE -ne 0) { throw "Could not grant Local Service read access to $directory." }
}
& $icacls $script /grant '*S-1-5-19:RX'
if ($LASTEXITCODE -ne 0) { throw 'Could not grant Local Service read access to the launcher script.' }
& $icacls $logDirectory /grant '*S-1-5-19:(OI)(CI)M'
if ($LASTEXITCODE -ne 0) { throw 'Could not grant Local Service access to the HotWater logs.' }

$action = New-ScheduledTaskAction -Execute $node -Argument ('"{0}"' -f $script) -WorkingDirectory $runtime
$principal = New-ScheduledTaskPrincipal -UserId $serviceAccount -LogonType ServiceAccount -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT30S'
$taskSettings = @{
    MultipleInstances = 'IgnoreNew'
    RestartCount = 5
    RestartInterval = New-TimeSpan -Minutes 1
    ExecutionTimeLimit = [TimeSpan]::Zero
    AllowStartIfOnBatteries = $true
    DontStopIfGoingOnBatteries = $true
}
$taskParameters = @{
    TaskPath = '\'
    TaskName = $taskName
    Action = $action
    Principal = $principal
    Trigger = $trigger
    Settings = New-ScheduledTaskSettingsSet @taskSettings
    Description = 'Launch the read-only HotWater stats on the Nest Hub from its Matter tile.'
}
$null = Register-ScheduledTask @taskParameters
Start-ScheduledTask -TaskPath '\' -TaskName $taskName
Write-Information "Started $taskName. Check $logFile for pairing readiness." -InformationAction Continue
