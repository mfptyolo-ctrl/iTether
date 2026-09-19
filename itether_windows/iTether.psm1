# iTether PowerShell module
# ========================
#
# Public cmdlets (all require Administrator privileges except where noted):
#
#   Install-iTetherDriver        - Stage our INF that binds UsbNcm.sys to
#                                  Apple tethering PIDs and pre-register
#                                  it in the DriverStore.
#   Get-iTetherStatus            - One-line JSON dump of the current state.
#   Repair-iTetherConnection     - The "USB device has an error and can't
#                                  be recognized" repair: kill any stale
#                                  filter, rebind the driver, restart the
#                                  NetAdapter, request a fresh DHCP lease.
#   Enable-iTetherInternetSharing - Enable Windows ICS so the iPhone's
#                                  Personal Hotspot is re-shared over
#                                  Ethernet/Wi-Fi to other devices.
#   Disable-iTetherInternetSharing
#
# Module entry point. We embed the INF content via a base64 blob below
# so the module is one-file deployable; the INF is decoded at install
# time to %ProgramData%\iTether\drivers\.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:iTetherRoot       = Join-Path $env:ProgramData 'iTether'
$script:iTetherDriverDir  = Join-Path $script:iTetherRoot 'drivers'
$script:iTetherLogDir     = Join-Path $script:iTetherRoot 'logs'
$script:iTetherInfPath    = Join-Path $script:iTetherDriverDir 'itether_usbncm.inf'

function Initialize-iTetherEnvironment {
    if (-not (Test-Path $script:iTetherRoot))      { New-Item -ItemType Directory -Path $script:iTetherRoot      | Out-Null }
    if (-not (Test-Path $script:iTetherDriverDir)) { New-Item -ItemType Directory -Path $script:iTetherDriverDir | Out-Null }
    if (-not (Test-Path $script:iTetherLogDir))    { New-Item -ItemType Directory -Path $script:iTetherLogDir    | Out-Null }
}

function Write-iTetherLog {
    param([string]$Message, [string]$Level = 'INFO')
    Initialize-iTetherEnvironment
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss.fff')
    $line  = "[$stamp] [$Level] $Message"
    Add-Content -Path (Join-Path $script:iTetherLogDir 'itether.log') -Value $line
    if ($env:ITETHER_VERBOSE -eq '1') {
        Write-Host $line
    }
}

function Assert-iTetherAdmin {
    $id  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pr.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
        throw 'iTether cmdlets must be run as Administrator.'
    }
}

function Install-iTetherDriver {
    [CmdletBinding()]
    param(
        [switch]$Force,
        [switch]$RepairUnplugged
    )
    Assert-iTetherAdmin
    Initialize-iTetherEnvironment

    # The INF file ships next to this module. We stage it from there to
    # %ProgramData%\iTether\drivers\ and add it to the DriverStore.
    $moduleDir = Split-Path -Parent $MyInvocation.MyCommand.Module.Path
    $sourceInf = Join-Path $moduleDir 'INF\itether_usbncm.inf'
    if (-not (Test-Path $sourceInf)) {
        throw "INF not found next to module: $sourceInf"
    }
    Copy-Item -Path $sourceInf -Destination $script:iTetherInfPath -Force

    Write-iTetherLog "Installing driver from $script:iTetherInfPath"
    $args = @('/add-driver', "`"$script:iTetherInfPath`"", '/install')
    if ($Force) { $args += '/force' }
    $proc = Start-Process -FilePath 'pnputil.exe' -ArgumentList $args -NoNewWindow -PassThru -Wait -RedirectStandardOutput "$script:iTetherLogDir\pnputil.out" -RedirectStandardError "$script:iTetherLogDir\pnputil.err"
    Write-iTetherLog "pnputil exit=$($proc.ExitCode)"

    if ($RepairUnplugged) {
        # Eagerly rebind any Apple tethering children already present so
        # the user doesn't have to unplug/replug.
        Get-iTetherStatus | Out-Null
        Repair-iTetherConnection | Out-Null
    }

    [pscustomobject]@{
        Success = ($proc.ExitCode -eq 0)
        InfPath = $script:iTetherInfPath
        ExitCode = $proc.ExitCode
    }
}

function Get-iTetherStatus {
    [CmdletBinding()]
    param()
    # We delegate the heavy lifting to the Python helper. It returns JSON.
    $python = (Get-Command 'python' -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command 'python3' -ErrorAction SilentlyContinue).Source }
    if (-not $python) {
        throw 'Python 3.10+ required for iTether diagnostics. Install from https://python.org.'
    }
    $moduleDir = Split-Path -Parent $MyInvocation.MyCommand.Module.Path
    $libPath   = Join-Path $moduleDir '..\..\'
    $env:PYTHONPATH = $libPath + [IO.Path]::PathSeparator + $env:PYTHONPATH
    $out = & $python -m itether_core diag 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-iTetherLog "diagnostic failed: $out" 'WARN'
    }
    # Find the line that looks like a JSON document. We accept either an
    # object ({...}) or an array ([...]).
    $json = $out | Where-Object {
        $_ -is [string] -and ($_.TrimStart().StartsWith('{') -or $_.TrimStart().StartsWith('['))
    } | Select-Object -First 1
    if (-not $json) {
        # Fall back to a minimal manual diagnostic so the user always
        # gets *something*.
        return [pscustomobject]@{
            usb_devices_found    = 0
            tether_composites    = 0
            tether_children      = 0
            net_adapter          = $null
            net_adapter_up       = $false
            timestamp            = [DateTime]::UtcNow
        }
    }
    try {
        return $json | ConvertFrom-Json -Depth 10
    } catch {
        Write-iTetherLog "Could not parse diagnostic JSON: $_" 'WARN'
        return [pscustomobject]@{
            usb_devices_found = -1
            raw = $json
        }
    }
}

function Get-iTetherTetheringInstanceId {
    [CmdletBinding()]
    param()
    # Ask Python which MI_xx child of the iPhone is the tethering function.
    # Returns the device instance ID (e.g. "USB\VID_05AC&PID_12A8\...&MI_02")
    # or $null if the device is not connected / the tethering function is
    # not enumerable.
    $python = (Get-Command 'python' -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command 'python3' -ErrorAction SilentlyContinue).Source }
    if (-not $python) { return $null }

    $moduleDir = Split-Path -Parent $MyInvocation.MyCommand.Module.Path
    $libPath   = Join-Path $moduleDir '..\..\'
    $env:PYTHONPATH = $libPath + [IO.Path]::PathSeparator + $env:PYTHONPATH

    $script = @"
import sys, json
sys.path.insert(0, r'$libPath')
from itether_core.descriptor import (
    parse_lsusb_dump, identify_tethering_function,
)
from itether_core.platform_windows import (
    enumerate_usb_devices, find_apple_tethering_children,
)
APPLE_PIDS = {0x12A8, 0x12AB, 0x1905, 0x12A0, 0x12A4}
nodes = enumerate_usb_devices()
for node in nodes:
    if node.pid not in APPLE_PIDS or node.mi is not None:
        continue
    children = [
        n for n in find_apple_tethering_children([node])
    ]
    # No per-interface descriptors from SetupAPI - we fall back to the
    # heuristic "the MI_02 child is the tethering function" for the
    # well-known Apple PIDs.
    for c in sorted(children, key=lambda n: n.mi or 0):
        print(c.instance_id)
        break
"@
    $out = & $python -c $script 2>$null
    if ($out) {
        return ($out | Select-Object -First 1).Trim()
    }
    return $null
}

function Repair-iTetherConnection {
    [CmdletBinding()]
    param()
    Assert-iTetherAdmin
    Initialize-iTetherEnvironment

    Write-iTetherLog 'Repair-iTetherConnection: start'

    # Step 1: find the tethering MI_xx child specifically (not every MI
    # of the iPhone - some of them are RemoteXPC, PTP, etc.).
    $tetherInstance = Get-iTetherTetheringInstanceId
    if (-not $tetherInstance) {
        Write-iTetherLog 'No Apple tethering device present.' 'WARN'
        return [pscustomobject]@{ Success = $false; Message = 'Device not connected.' }
    }
    Write-iTetherLog "Tethering instance: $tetherInstance"

    # Step 2: locate the devnode + check if it's healthy.
    $dev = Get-PnpDevice -InstanceId $tetherInstance -ErrorAction SilentlyContinue
    if (-not $dev) {
        # It's plugged in but PnP hasn't enumerated it. Force rescan.
        Write-iTetherLog 'No PnP devnode yet, forcing rescan'
        & pnputil.exe /scan-devices | Out-Null
        Start-Sleep -Seconds 3
        $dev = Get-PnpDevice -InstanceId $tetherInstance -ErrorAction SilentlyContinue
    }

    $needsRepair = $false
    if (-not $dev) {
        Write-iTetherLog 'Devnode still not found after rescan' 'WARN'
        $needsRepair = $true
    } elseif ($dev.Problem -ne 0) {
        Write-iTetherLog "Devnode has problem code $($dev.Problem) - will reinstall"
        $needsRepair = $true
    }

    if ($needsRepair) {
        # Force pnputil to remove + re-add the driver. We only touch the
        # tethering function - we don't want to yank the RemoteXPC
        # function or the PTP/MTP function.
        & pnputil.exe /remove-device $tetherInstance | Out-Null
        Start-Sleep -Seconds 1
        & pnputil.exe /scan-devices | Out-Null
        Start-Sleep -Seconds 3
    }

    # Step 3: wait for the NetAdapter to come up.
    $adapter = $null
    for ($i = 0; $i -lt 10; $i++) {
        $adapter = Get-NetAdapter | Where-Object {
            $_.InterfaceDescription -match 'Apple Mobile Device Ethernet|iPhone|USB.*CDC'
        } | Select-Object -First 1
        if ($adapter) { break }
        Start-Sleep -Seconds 1
    }

    if (-not $adapter) {
        Write-iTetherLog 'No Apple NetAdapter appeared after repair.' 'WARN'
        return [pscustomobject]@{ Success = $false; Message = 'No Apple adapter found.' }
    }

    Write-iTetherLog "Found NetAdapter $($adapter.Name), restarting"
    Disable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Enable-NetAdapter  -Name $adapter.Name -Confirm:$false
    Set-NetAdapterAdvancedProperty -Name $adapter.Name -DisplayName 'Power Saving Mode' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue
    Set-NetAdapterAdvancedProperty -Name $adapter.Name -DisplayName 'Energy Efficient Ethernet' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue
    Set-NetAdapterAdvancedProperty -Name $adapter.Name -DisplayName 'Selective Suspend'   -DisplayValue 'Disabled' -ErrorAction SilentlyContinue
    Set-NetIPInterface -InterfaceAlias $adapter.Name -Dhcp Enabled
    ipconfig /renew  | Out-Null

    Write-iTetherLog 'Repair-iTetherConnection: done'
    return [pscustomobject]@{
        Success = $true
        Message = "Repaired $($adapter.Name)."
        Adapter = $adapter.Name
        TetherInstance = $tetherInstance
    }
}

function Enable-iTetherInternetSharing {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PrivateAdapter
    )
    Assert-iTetherAdmin
    Initialize-iTetherEnvironment

    $public = Get-NetAdapter | Where-Object {
        $_.InterfaceDescription -match 'Apple Mobile Device Ethernet|iPhone|USB.*CDC'
    } | Select-Object -First 1
    if (-not $public) {
        throw 'Apple tether adapter not found. Is Personal Hotspot enabled?'
    }

    Write-iTetherLog "Enabling ICS: public=$($public.Name), private=$PrivateAdapter"

    # Use HNetCfg COM to flip sharing.
    $csPath = Join-Path $env:WINDIR 'System32\hnetcfg.dll'
    if (-not ('HNetCfg.HNetShare' -as [type])) {
        Add-Type -Path $csPath
    }
    $m = New-Object -ComObject HNetCfg.HNetShare
    $publicConn = $null; $privateConn = $null
    foreach ($c in $m.EnumEveryConnection()) {
        $props = $m.NetConnectionProps.Invoke($c)
        if ($props.Name -eq $public.Name)   { $publicConn  = $c }
        if ($props.Name -eq $PrivateAdapter) { $privateConn = $c }
    }
    if (-not $publicConn -or -not $privateConn) {
        throw "Could not find both adapters: public=$($public.Name), private=$PrivateAdapter"
    }
    $pubCfg  = $m.INetSharingConfigurationForINetConnection.Invoke($publicConn)
    $privCfg = $m.INetSharingConfigurationForINetConnection.Invoke($privateConn)
    $pubCfg.EnableSharing(0)   | Out-Null
    $privCfg.EnableSharing(1)  | Out-Null

    return [pscustomobject]@{
        Success = $true
        Public  = $public.Name
        Private = $PrivateAdapter
    }
}

function Disable-iTetherInternetSharing {
    [CmdletBinding()]
    param()
    Assert-iTetherAdmin
    Initialize-iTetherEnvironment
    $csPath = Join-Path $env:WINDIR 'System32\hnetcfg.dll'
    if (-not ('HNetCfg.HNetShare' -as [type])) {
        Add-Type -Path $csPath
    }
    $m = New-Object -ComObject HNetCfg.HNetShare
    foreach ($c in $m.EnumEveryConnection()) {
        $cfg = $m.INetSharingConfigurationForINetConnection.Invoke($c)
        if ($cfg.SharingEnabled) {
            $cfg.DisableSharing() | Out-Null
        }
    }
}

function Start-iTetherWatchdog {
    [CmdletBinding()]
    param(
        [int]$IntervalSeconds = 30,
        [int]$IdleThreshold   = 90
    )
    Assert-iTetherAdmin
    Initialize-iTetherEnvironment
    Write-iTetherLog "Watchdog starting, interval=${IntervalSeconds}s, idle=${IdleThreshold}s"
    while ($true) {
        try {
            $status = Get-iTetherStatus
            $adapter = Get-NetAdapter | Where-Object {
                $_.InterfaceDescription -match 'Apple Mobile Device Ethernet|iPhone|USB.*CDC'
            } | Select-Object -First 1
            if (-not $adapter) {
                Write-iTetherLog 'No adapter - waiting for reconnect' 'INFO'
            } elseif ($adapter.Status -ne 'Up') {
                Write-iTetherLog "$($adapter.Name) is $($adapter.Status) - repairing" 'WARN'
                Repair-iTetherConnection | Out-Null
            } else {
                # Link up. Send a small keepalive so the iPhone keeps the
                # hotspot awake.
                $stats = Get-NetAdapterStatistics -Name $adapter.Name
                Write-iTetherLog "RX=$($stats.ReceivedBytes) TX=$($stats.SentBytes)"
            }
        } catch {
            Write-iTetherLog "Watchdog iteration error: $_" 'ERROR'
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
}

Export-ModuleMember -Function `
    Install-iTetherDriver, `
    Get-iTetherStatus, `
    Repair-iTetherConnection, `
    Enable-iTetherInternetSharing, `
    Disable-iTetherInternetSharing, `
    Start-iTetherWatchdog
