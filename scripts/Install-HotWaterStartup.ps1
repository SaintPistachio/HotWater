#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$taskRunner = Join-Path $PSScriptRoot 'run-hotwater-task.cmd'
$logDirectory = 'C:\HotWaterRuntime\logs'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$serviceSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-19')
$serviceAccount = $serviceSid.Translate([Security.Principal.NTAccount]).Value
$icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'
$command = [Security.SecurityElement]::Escape((Join-Path $env:SystemRoot 'System32\cmd.exe'))
$workingDirectory = [Security.SecurityElement]::Escape($projectRoot)

foreach ($relativePath in @('.runtime\python313\hotwater-ready.txt', '.runtime\python313\python.exe', 'run-relay.cmd', 'gauge.py',
        'gauge.html', 'hotwater_analytics.py',
        'relay-src\relay\icomm_hub.py', 'certs\aylanetworks.com.crt', 'certs\aylanetworks.com.key')) {
    $inputPath = Join-Path $projectRoot $relativePath
    if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) {
        throw "HotWater startup requires $inputPath."
    }
}
if (-not (Test-Path -LiteralPath $taskRunner -PathType Leaf)) {
    throw "HotWater startup requires $taskRunner."
}

$tasks = foreach ($component in @('Relay', 'Gauge')) {
    $name = "HotWater-$component"
    $actionArguments = '/d /c ""{0}" {1}"' -f $taskRunner, $component.ToLowerInvariant()
    $arguments = [Security.SecurityElement]::Escape($actionArguments)
    [pscustomobject]@{
        Name = $name
        Xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Start the HotWater $component on the office PC.</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Repetition><Interval>PT1M</Interval></Repetition>
      <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
      <Delay>PT30S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="HotWaterService">
      <UserId>$($serviceSid.Value)</UserId>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="HotWaterService">
    <Exec>
      <Command>$command</Command>
      <Arguments>$arguments</Arguments>
      <WorkingDirectory>$workingDirectory</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
    }
}

Write-Information "[$(Get-Date -Format o)] HotWater startup on $env:COMPUTERNAME for $serviceAccount" -InformationAction Continue
Write-Information "Project: $projectRoot" -InformationAction Continue
Write-Information "Runtime logs: $logDirectory" -InformationAction Continue
if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, 'Register HotWater-Relay and HotWater-Gauge')) {
    foreach ($task in $tasks) {
        Write-Information "Task definition for $($task.Name)" -InformationAction Continue
        Write-Output $task.Xml
    }
    return
}

$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this setup from an elevated PowerShell 7 window.'
}

$existing = @(Get-ScheduledTask -TaskPath '\' |
    Where-Object { $_.TaskName -in $tasks.Name })
if ($existing.Count -gt 0) {
    throw "Task name already in use: $($existing.TaskName -join ', '). Inspect the existing task in Task Scheduler."
}

$registered = [Collections.Generic.List[string]]::new()
$savedAccess = [ordered]@{}
try {
    $null = New-Item -ItemType Directory -Path 'C:\HotWaterRuntime\logs' -Force
    $serviceAccess = @(
        @{ Path = (Join-Path $projectRoot '.runtime\python313'); Rights = '(OI)(CI)RX' }
        @{ Path = (Join-Path $projectRoot 'relay-src\relay'); Rights = '(OI)(CI)RX' }
        @{ Path = (Join-Path $projectRoot 'run-relay.cmd'); Rights = 'RX' }
        @{ Path = (Join-Path $projectRoot 'gauge.py'); Rights = 'RX' }
        @{ Path = (Join-Path $projectRoot 'gauge.html'); Rights = 'R' }
        @{ Path = (Join-Path $projectRoot 'hotwater_analytics.py'); Rights = 'R' }
        @{ Path = $taskRunner; Rights = 'RX' }
        @{ Path = (Join-Path $projectRoot 'certs\aylanetworks.com.crt'); Rights = 'R' }
        @{ Path = (Join-Path $projectRoot 'certs\aylanetworks.com.key'); Rights = 'R' }
        @{ Path = (Join-Path $projectRoot 'data'); Rights = '(OI)(CI)M' }
        @{ Path = $logDirectory; Rights = '(OI)(CI)M' }
    )
    $dhParameters = Join-Path $projectRoot 'certs\dhparams.pem'
    if (Test-Path -LiteralPath $dhParameters -PathType Leaf) {
        $serviceAccess += @{ Path = $dhParameters; Rights = 'R' }
    }
    foreach ($grant in $serviceAccess) {
        $acl = Get-Acl -LiteralPath $grant.Path
        $savedAccess[$grant.Path] = $acl.GetSecurityDescriptorSddlForm('Access')
        & $icacls $grant.Path /grant ('*{0}:{1}' -f $serviceSid.Value, $grant.Rights)
        if ($LASTEXITCODE -ne 0) { throw "Could not grant Local Service access to $($grant.Path)." }
    }

    $taskService = New-Object -ComObject 'Schedule.Service'
    $taskService.Connect()
    $taskFolder = $taskService.GetFolder('\')
    foreach ($task in $tasks) {
        $definition = $taskService.NewTask(0)
        $definition.XmlText = $task.Xml
        $definition.Principal.LogonType = 5 # TASK_LOGON_SERVICE_ACCOUNT
        Write-Information "Registering $($task.Name) for $serviceAccount." -InformationAction Continue
        # TASK_CREATE leaves any task with the same name intact.
        $null = $taskFolder.RegisterTaskDefinition($task.Name, $definition, 2, $serviceSid.Value, $null, 5, $null)
        $registered.Add($task.Name)
        $installed = Get-ScheduledTask -TaskPath '\' -TaskName $task.Name
        Write-Information "Registered $($installed.TaskName) for $($installed.Principal.UserId) ($($installed.State))." -InformationAction Continue
    }
}
catch {
    foreach ($name in $registered) {
        try {
            Unregister-ScheduledTask -TaskPath '\' -TaskName $name -Confirm:$false
            Write-Information "Rolled back $name." -InformationAction Continue
        }
        catch {
            Write-Warning "Could not roll back $name. Inspect it in Task Scheduler. $($_.Exception.Message)"
        }
    }
    foreach ($path in $savedAccess.Keys) {
        try {
            $acl = Get-Acl -LiteralPath $path
            $acl.SetSecurityDescriptorSddlForm($savedAccess[$path], 'Access')
            Set-Acl -LiteralPath $path -AclObject $acl
        }
        catch {
            Write-Warning "Could not restore the previous permissions on $path. $($_.Exception.Message)"
        }
    }
    throw
}

Write-Information 'Both startup tasks are registered. Follow the cutover and verification steps in SETUP.md.' -InformationAction Continue
