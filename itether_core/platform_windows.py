"""
Windows-only helpers used by the tray app and the PowerShell module.

We deliberately keep this layer thin and ctypes-only so it works on stock
Python 3.11 without third-party packages. Everything here must be safe to
call only when ``sys.platform.startswith('win')``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class UsbDeviceNode:
    """A USB device node as enumerated by SetupAPI / SetupDi."""

    instance_id: str
    hardware_id: str
    description: str = ""
    status_code: int = 0  # CONFIGRET status from CM_Get_DevNode_Status
    problem_code: int = 0  # CONFIGRET problem code (e.g. CM_PROB_DRIVER_FAILED)

    @property
    def vid(self) -> int:
        m = re.search(r"VID_([0-9A-Fa-f]{4})", self.instance_id)
        return int(m.group(1), 16) if m else 0

    @property
    def pid(self) -> int:
        m = re.search(r"PID_([0-9A-Fa-f]{4})", self.instance_id)
        return int(m.group(1), 16) if m else 0

    @property
    def mi(self) -> Optional[int]:
        m = re.search(r"MI_(\d{2})", self.instance_id)
        return int(m.group(1)) if m else None

    @property
    def is_apple(self) -> bool:
        return self.vid == 0x05AC


@dataclass
class NetAdapter:
    """A Windows network adapter as reported by ``Get-NetAdapter``."""

    name: str
    if_index: int
    status: str
    interface_description: str = ""
    driver_name: str = ""
    mac: str = ""

    @property
    def is_up(self) -> bool:
        return self.status.lower() in ("up", "connected")

    @property
    def is_apple_tether(self) -> bool:
        # iTunes/Apple ships adapters named like "Apple Mobile Device Ethernet"
        # or "iPhone Tether" - match either way. Also match our own adapter
        # once UsbNcm.sys has bound, which will inherit the iPhone product
        # string "Apple Mobile Device Ethernet".
        text = (self.name + " " + self.interface_description).lower()
        return (
            "apple mobile device ethernet" in text
            or "iphone tether" in text
            or "apple usb ethernet" in text
        )


# ---------------------------------------------------------------------------
# SetupAPI enumeration (composite + MI_xx children)
# ---------------------------------------------------------------------------


def enumerate_usb_devices() -> List[UsbDeviceNode]:
    """Enumerate all present USB devices via SetupAPI.

    The returned list contains composite devices *and* their MI_xx children
    - we filter the latter up by their instance ID pattern. Callers should
    use :func:`find_apple_devices` to scope the search.
    """
    if not sys.platform.startswith("win"):
        return []

    import ctypes
    from ctypes import wintypes

    setupapi = ctypes.WinDLL("setupapi")
    cfgmgr = ctypes.WinDLL("cfgmgr32")

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    USB_CLASS_GUID = GUID(
        0x36FC9E60, 0xC465, 0x11CF,
        (ctypes.c_ubyte * 8)(0x80, 0x56, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00),
    )

    DIGCF_PRESENT = 0x00000002
    DIGCF_DEVICEINTERFACE = 0x00000010

    SetupDiGetClassDevs = setupapi.SetupDiGetClassDevsW
    SetupDiGetClassDevs.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        ctypes.c_uint32,
    ]
    SetupDiGetClassDevs.restype = wintypes.HANDLE

    SetupDiEnumDeviceInfo = setupapi.SetupDiEnumDeviceInfo
    SetupDiEnumDeviceInfo.argtypes = [wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p]
    SetupDiEnumDeviceInfo.restype = ctypes.c_int

    SetupDiGetDeviceInstanceId = setupapi.SetupDiGetDeviceInstanceIdW
    SetupDiGetDeviceInstanceId.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    SetupDiGetDeviceInstanceId.restype = ctypes.c_int

    SetupDiGetDeviceRegistryProperty = setupapi.SetupDiGetDeviceRegistryPropertyW
    SetupDiGetDeviceRegistryProperty.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    SetupDiGetDeviceRegistryProperty.restype = ctypes.c_int

    SetupDiDestroyDeviceInfoList = setupapi.SetupDiDestroyDeviceInfoList
    SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    SetupDiDestroyDeviceInfoList.restype = ctypes.c_int

    CM_Get_DevNode_Status = cfgmgr.CM_Get_DevNode_Status
    CM_Get_DevNode_Status.argtypes = [
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    CM_Get_DevNode_Status.restype = ctypes.c_int

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint32),
            ("ClassGuid", GUID),
            ("DevInst", ctypes.c_uint32),
            ("Reserved", ctypes.c_void_p),
        ]

    handle = SetupDiGetClassDevs(
        ctypes.byref(USB_CLASS_GUID),
        None,
        None,
        DIGCF_PRESENT | DIGCF_DEVICEINTERFACE,
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        return []

    out: List[UsbDeviceNode] = []
    try:
        idx = 0
        while True:
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not SetupDiEnumDeviceInfo(handle, idx, ctypes.byref(info)):
                break
            idx += 1

            bufsize = ctypes.c_uint32(512)
            buf = ctypes.create_unicode_buffer(bufsize.value)
            ok = SetupDiGetDeviceInstanceId(
                handle,
                ctypes.byref(info),
                buf,
                bufsize,
                ctypes.byref(bufsize),
            )
            if not ok:
                continue
            inst = buf.value

            # hardware ID: read SPDRP_HARDWAREID (1)
            regbufsize = ctypes.c_uint32(1024)
            regbuf = (ctypes.c_byte * regbufsize.value)()
            ok = SetupDiGetDeviceRegistryProperty(
                handle,
                ctypes.byref(info),
                1,  # SPDRP_HARDWAREID
                None,
                ctypes.byref(regbuf),
                regbufsize,
                ctypes.byref(regbufsize),
            )
            hardware_id = ""
            if ok:
                try:
                    hardware_id = ctypes.wstring_at(ctypes.addressof(regbuf))
                except Exception:
                    hardware_id = ""

            # description: SPDRP_DEVICEDESC (0)
            descbuf = (ctypes.c_byte * 1024)()
            descsize = ctypes.c_uint32(1024)
            ok = SetupDiGetDeviceRegistryProperty(
                handle,
                ctypes.byref(info),
                0,  # SPDRP_DEVICEDESC
                None,
                ctypes.byref(descbuf),
                descsize,
                ctypes.byref(descsize),
            )
            description = ""
            if ok:
                try:
                    description = ctypes.wstring_at(ctypes.addressof(descbuf))
                except Exception:
                    description = ""

            status_code = 0
            problem_code = 0
            ulStatus = ctypes.c_ulong(0)
            ulProblem = ctypes.c_ulong(0)
            rc = CM_Get_DevNode_Status(
                ctypes.byref(ulStatus),
                ctypes.byref(ulProblem),
                info.DevInst,
                0,
            )
            if rc == 0:
                status_code = ulStatus.value
                problem_code = ulProblem.value

            out.append(
                UsbDeviceNode(
                    instance_id=inst,
                    hardware_id=hardware_id,
                    description=description,
                    status_code=status_code,
                    problem_code=problem_code,
                )
            )
    finally:
        SetupDiDestroyDeviceInfoList(handle)

    return out


def find_apple_devices(nodes: Optional[Sequence[UsbDeviceNode]] = None) -> List[UsbDeviceNode]:
    nodes = nodes if nodes is not None else enumerate_usb_devices()
    return [n for n in nodes if n.is_apple]


# ---------------------------------------------------------------------------
# Driver binding
# ---------------------------------------------------------------------------


# Apple iPhone/iPad product IDs that we know expose a CDC-NCM tethering
# function on one of their MI_xx child devnodes.
APPLE_TETHERING_PIDS = {0x12A8, 0x12AB, 0x1905}


def find_apple_tethering_children(nodes: Optional[Sequence[UsbDeviceNode]] = None) -> List[UsbDeviceNode]:
    """Return the MI_xx child devnodes of an Apple tethering device."""
    nodes = nodes if nodes is not None else enumerate_usb_devices()
    composites = [
        n for n in nodes
        if n.is_apple and n.pid in APPLE_TETHERING_PIDS and n.mi is None
    ]
    base_prefixes = []
    for c in composites:
        # Composite instance id looks like "USB\\VID_05AC&PID_12A8\\0123..."
        m = re.match(r"^(USB\\VID_[0-9A-Fa-f]{4}&PID_[0-9A-Fa-f]{4})", c.instance_id)
        if m:
            base_prefixes.append(m.group(1))

    if not base_prefixes:
        return []

    out = []
    for n in nodes:
        for base in base_prefixes:
            if n.instance_id.startswith(base + "\\") and n.mi is not None:
                out.append(n)
    return out


def list_current_driver(instance_id: str) -> str:
    """Return the LowerFilters / UpperFilters / service name binding a node.

    Uses pnputil to dump the driver store and pulls the device's currently
    bound service.
    """
    cmd = ["pnputil", "/enum-devices", "/instanceid", instance_id, "/connected"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    for line in out.splitlines():
        if line.strip().startswith("ServiceName:"):
            return line.split(":", 1)[1].strip()
    return ""


def bind_driver_to_instance(
    instance_id: str,
    inf_path: str,
    *,
    force: bool = False,
    reboot_if_required: bool = False,
) -> Tuple[bool, str]:
    """Install ``inf_path`` and bind it to ``instance_id``.

    Returns ``(success, message)``. Requires administrator privileges.
    """
    inf_path = os.path.abspath(inf_path)
    if not os.path.isfile(inf_path):
        return False, f"INF not found: {inf_path}"

    cmd = ["pnputil", "/add-driver", inf_path, "/install"]
    if force:
        cmd.append("/force")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, "pnputil not found - are you on Windows?"
    if proc.returncode != 0:
        return False, f"pnputil failed: {proc.stdout}\n{proc.stderr}"

    # Now update the specific instance to use the new driver.
    cmd2 = ["pnputil", "/scan-devices"]  # cheap, lets PnP re-evaluate
    if reboot_if_required:
        cmd2.append("/reboot")
    subprocess.run(cmd2, capture_output=True, text=True, timeout=30)

    # Drive setupapi directly to update the device's driver.
    cmd3 = [
        "powershell",
        "-NoProfile",
        "-Command",
        f"pnputil /update-device '{instance_id}'",
    ]
    proc3 = subprocess.run(cmd3, capture_output=True, text=True, timeout=30)
    return proc3.returncode == 0, proc3.stdout + "\n" + proc3.stderr


# ---------------------------------------------------------------------------
# Network adapter management (PowerShell wrapper)
# ---------------------------------------------------------------------------


def list_net_adapters() -> List[NetAdapter]:
    """List every present NetAdapter via PowerShell."""
    if not sys.platform.startswith("win"):
        return []
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-NetAdapter | Select-Object Name, ifIndex, Status, InterfaceDescription, DriverName, MacAddress | ConvertTo-Json -Compress",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if not out:
        return []
    import json

    if out.startswith("["):
        items = json.loads(out)
    else:
        items = [json.loads(out)]
    out_list = []
    for it in items:
        out_list.append(
            NetAdapter(
                name=it.get("Name", "") or "",
                if_index=int(it.get("ifIndex", 0) or 0),
                status=it.get("Status", "") or "",
                interface_description=it.get("InterfaceDescription", "") or "",
                driver_name=it.get("DriverName", "") or "",
                mac=it.get("MacAddress", "") or "",
            )
        )
    return out_list


def find_apple_tether_adapter() -> Optional[NetAdapter]:
    for a in list_net_adapters():
        if a.is_apple_tether:
            return a
    return None


def enable_adapter(name: str) -> bool:
    cmd = ["powershell", "-NoProfile", "-Command", f"Enable-NetAdapter -Name '{name}' -Confirm:$false"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def disable_adapter(name: str) -> bool:
    cmd = ["powershell", "-NoProfile", "-Command", f"Disable-NetAdapter -Name '{name}' -Confirm:$false"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def restart_adapter(name: str) -> bool:
    cmd = ["powershell", "-NoProfile", "-Command", f"Restart-NetAdapter -Name '{name}' -Confirm:$false"]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def set_maximum_performance(name: str) -> bool:
    """Disable power-saving on the network adapter so the link never sleeps."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            f"Disable-NetAdapterPowerManagement -Name '{name}' -ErrorAction SilentlyContinue;"
            f"Set-NetAdapterAdvancedProperty -Name '{name}' -DisplayName 'Power Saving Mode' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue;"
            f"Set-NetAdapterAdvancedProperty -Name '{name}' -DisplayName 'Energy Efficient Ethernet' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue;"
            f"Set-NetAdapterAdvancedProperty -Name '{name}' -DisplayName 'Selective Suspend' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue;"
            f"Set-NetAdapterAdvancedProperty -Name '{name}' -DisplayName 'Auto Disable Gigabit' -DisplayValue 'Disabled' -ErrorAction SilentlyContinue;"
        ),
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def set_interface_mtu(name: str, mtu: int) -> bool:
    """Set the MTU of the adapter (helps NCM bulk throughput)."""
    cmd = [
        "netsh",
        "interface",
        "ipv4",
        "set",
        "subinterface",
        name,
        f"mtu={mtu}",
        "store=persistent",
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def enable_dhcp(name: str) -> bool:
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        f"Set-NetIPInterface -InterfaceAlias '{name}' -Dhcp Enabled",
    ]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Internet Connection Sharing (ICS) via HNetCfg
# ---------------------------------------------------------------------------


def enable_ics(public_name: str, private_name: str) -> bool:
    """Enable ICS from ``public_name`` to ``private_name`` (private becomes the LAN side).

    In the iPhone-tether scenario:
      * public_name  = "Apple Mobile Device Ethernet" (the iPhone link)
      * private_name = Wi-Fi/Ethernet that other devices will join
    """
    if not sys.platform.startswith("win"):
        return False
    ps = (
        "$ErrorActionPreference = 'Stop'\n"
        # Register HNetCfg COM interface (built-in on Windows).
        "$src = $env:WINDIR + '\\System32\\hnetcfg.dll'\n"
        "if (-not ([System.Management.Automation.PSTypeName]'HNetCfg.HNetShare').Type) {\n"
        "  $src = [System.IO.Path]::GetFullPath($src)\n"
        "  Add-Type -Path $src\n"
        "}\n"
        "$m = New-Object -ComObject HNetCfg.HNetShare\n"
        "$conns = $m.EnumEveryConnection\n"
        "$publicConn = $null; $privateConn = $null\n"
        "foreach ($c in $conns) {\n"
        "  $props = $m.NetConnectionProps.Invoke($c)\n"
        "  if ($props.Name -eq $args[0]) { $publicConn = $c }\n"
        "  if ($props.Name -eq $args[1]) { $privateConn = $c }\n"
        "}\n"
        "if (-not $publicConn -or -not $privateConn) { throw 'connections not found' }\n"
        "$pub = $m.INetSharingConfigurationForINetConnection.Invoke($publicConn)\n"
        "$priv = $m.INetSharingConfigurationForINetConnection.Invoke($privateConn)\n"
        "$pub.EnableSharing(0)\n"
        "$priv.EnableSharing(1)\n"
    )
    cmd = ["powershell", "-NoProfile", "-Command", ps, public_name, private_name]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def disable_ics_all() -> bool:
    if not sys.platform.startswith("win"):
        return False
    ps = (
        "$ErrorActionPreference = 'Stop'\n"
        "if (-not ([System.Management.Automation.PSTypeName]'HNetCfg.HNetShare').Type) {\n"
        "  Add-Type -Path ($env:WINDIR + '\\System32\\hnetcfg.dll')\n"
        "}\n"
        "$m = New-Object -ComObject HNetCfg.HNetShare\n"
        "foreach ($c in $m.EnumEveryConnection) {\n"
        "  $cfg = $m.INetSharingConfigurationForINetConnection.Invoke($c)\n"
        "  if ($cfg.SharingEnabled) { $cfg.DisableSharing() }\n"
        "}\n"
    )
    cmd = ["powershell", "-NoProfile", "-Command", ps]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Watchdog helpers
# ---------------------------------------------------------------------------


def get_default_gateway_for(adapter_name: str) -> Optional[str]:
    """Return the IPv4 default gateway of the Apple tether adapter, if any."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            f"Get-NetRoute -InterfaceAlias '{adapter_name}' -AddressFamily IPv4 "
            f"-DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
            "Select-Object -First 1 -ExpandProperty NextHop"
        ),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out or None


def get_adapter_byte_counters(adapter_name: str) -> Tuple[int, int]:
    """Return (rx_bytes, tx_bytes) for the given adapter (live counters)."""
    ps = (
        f"Get-NetAdapterStatistics -Name '{adapter_name}' | "
        "Select-Object @{n='rx';e={[int64]$_.ReceivedBytes}},"
        " @{n='tx';e={[int64]$_.SentBytes}} | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0, 0
    if not out:
        return 0, 0
    try:
        j = json.loads(out)
    except json.JSONDecodeError:
        return 0, 0
    return int(j.get("rx", 0) or 0), int(j.get("tx", 0) or 0)


# ---------------------------------------------------------------------------
# High level: "is the device usable?"
# ---------------------------------------------------------------------------


def diagnose_apple_tether() -> dict:
    """Top-level helper used by both the tray app and the PowerShell module.

    Returns a dict suitable for direct JSON serialisation so the tray app
    can pop up a one-line status.
    """
    devices = find_apple_devices()
    tether_devices = [d for d in devices if d.pid in APPLE_TETHERING_PIDS]
    children = find_apple_tethering_children(devices)
    children_with_problem = [d for d in children if d.problem_code != 0]

    adapter = find_apple_tether_adapter()
    return {
        "usb_devices_found": len(devices),
        "tether_composites_found": len(tether_devices),
        "tether_children_found": len(children),
        "tether_children_with_problem": len(children_with_problem),
        "problem_children": [
            {
                "instance_id": d.instance_id,
                "problem_code": d.problem_code,
                "description": d.description,
            }
            for d in children_with_problem
        ],
        "net_adapter": (adapter.name if adapter else None),
        "net_adapter_up": adapter.is_up if adapter else False,
        "timestamp": time.time(),
    }


__all__ = [
    "UsbDeviceNode",
    "NetAdapter",
    "APPLE_TETHERING_PIDS",
    "enumerate_usb_devices",
    "find_apple_devices",
    "find_apple_tethering_children",
    "list_current_driver",
    "bind_driver_to_instance",
    "list_net_adapters",
    "find_apple_tether_adapter",
    "enable_adapter",
    "disable_adapter",
    "restart_adapter",
    "set_maximum_performance",
    "set_interface_mtu",
    "enable_dhcp",
    "enable_ics",
    "disable_ics_all",
    "get_default_gateway_for",
    "get_adapter_byte_counters",
    "diagnose_apple_tether",
]
