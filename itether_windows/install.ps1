<#
.SYNOPSIS
    Installer / bootstrapper for the iTether Windows package.

.DESCRIPTION
    Copies the iTether PowerShell module + INF into %ProgramData%\iTether,
    registers an AutoStart scheduled task that boots the tray app at logon,
    installs our INF into the driver store, and starts the tray app once.

    Re-run with -Uninstall to undo everything.
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoTask,
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'
$root      = Join-Path $env:ProgramData 'iTether'
$psm1      = Join-Path $root 'iTether.psm1'
$infSrc    = Join-Path $PSScriptRoot 'INF\itether_usbncm.inf'
$infDst    = Join-Path $root 'drivers\itether_usbncm.inf'
$logDir    = Join-Path $root 'logs'
$trayExe   = Join-Path $root 'iTetherTray.exe'
$traySrc   = Join-Path $PSScriptRoot 'iTether-Tray\bin\Release\net10.0-windows10.0.17763.0\win-x64\publish\iTetherTray.exe'
$taskName  = 'iTether.AutoStart'

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pr.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
        throw 'Run this installer from an elevated PowerShell.'
    }
}

Assert-Admin

if ($Uninstall) {
    Write-Host "Removing iTether..."
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-Process iTetherTray -ErrorAction SilentlyContinue | Stop-Process -Force
    if (Test-Path $root) { Remove-Item -Recurse -Force $root }
    Write-Host "Done. Driver INF stays in DriverStore - remove with: pnputil /delete-driver itether_usbncm.inf"
    exit 0
}

# === Stage files ===

if (-not (Test-Path $root))   { New-Item -ItemType Directory -Path $root   | Out-Null }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$driverDir = Join-Path $root 'drivers'
if (-not (Test-Path $driverDir)) { New-Item -ItemType Directory -Path $driverDir | Out-Null }

Copy-Item -Path (Join-Path $PSScriptRoot 'iTether.psm1') -Destination $psm1 -Force
Copy-Item -Path $infSrc -Destination $infDst -Force

if (Test-Path $traySrc) {
    Copy-Item -Path $traySrc -Destination $trayExe -Force
} else {
    Write-Warning "Tray exe not found at $traySrc - publish the .NET project first."
}

# === Driver store ===

Write-Host "Installing driver INF..."
$proc = Start-Process -FilePath 'pnputil.exe' -ArgumentList @('/add-driver', "`"$infDst`"", '/install') -NoNewWindow -PassThru -Wait
if ($proc.ExitCode -ne 0) {
    Write-Warning "pnputil returned $($proc.ExitCode) - driver install may have failed; check logs."
}

# === Scheduled task (auto-start tray) ===

if (-not $NoTask) {
    Write-Host "Creating auto-start task..."
    $action  = New-ScheduledTaskAction -Execute $trayExe
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0)
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
}

# === Start the tray app ===

if (Test-Path $trayExe) {
    if (-not $Silent) {
        Start-Process -FilePath $trayExe
    }
}

Write-Host ""
Write-Host "iTether installed. The tray icon should appear within a few seconds."
Write-Host "Run with -Uninstall to remove."
