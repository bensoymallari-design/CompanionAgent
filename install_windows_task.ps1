param(
    [string]$TaskName = "CompanionAgent Resolume Media Sync",
    [string]$ScriptPath = "",
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [switch]$AtStartup,
    [switch]$RunAsSystem
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath {
    param([string]$PathValue)
    $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PathValue)
}

function Find-Python {
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @{
            Execute = $pyLauncher.Source
            Prefix = "-3 "
        }
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            Execute = $python.Source
            Prefix = ""
        }
    }

    throw "Python was not found. Install Python 3.10+ and make sure py.exe or python.exe is on PATH."
}

if ([string]::IsNullOrWhiteSpace($ScriptPath)) {
    $ScriptPath = Join-Path $PSScriptRoot "resolume_media_agent.py"
}

$ScriptPath = Resolve-FullPath $ScriptPath
$ConfigPath = Resolve-FullPath $ConfigPath
$WorkingDirectory = Split-Path -Parent $ScriptPath

if (!(Test-Path $ScriptPath)) {
    throw "Agent script not found: $ScriptPath"
}

if (!(Test-Path $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

$python = Find-Python
$arguments = "$($python.Prefix)-u `"$ScriptPath`" --config `"$ConfigPath`""

$action = New-ScheduledTaskAction `
    -Execute $python.Execute `
    -Argument $arguments `
    -WorkingDirectory $WorkingDirectory

if ($AtStartup) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

if ($RunAsSystem) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Syncs server media into a local folder for Resolume Arena." `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Python: $($python.Execute)"
Write-Host "Script: $ScriptPath"
Write-Host "Config: $ConfigPath"
Write-Host "Trigger: $(if ($AtStartup) { 'At startup' } else { 'At logon' })"
