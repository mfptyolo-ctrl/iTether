"""
Live USB device probe for Apple iPhone/iPad tethering.

This module discovers Apple composite devices on the host's USB bus and
returns their live descriptors in the same :class:`AppleDeviceInfo` shape
that :mod:`itether_core.descriptor` produces for offline dumps.

The probe uses platform-specific backends:

* Linux:   parses ``/sys/bus/usb/devices/`` - no external deps, no root
* Windows: enumerates via SetupAPI through ``ctypes`` against ``setupapi.dll``
* macOS:   parses ``system_profiler SPUSBDataType`` (used for development
           on Mac and not part of the runtime on Windows/Linux)
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .descriptor import (
    AppleDeviceInfo,
    InterfaceDescriptor,
    TetheringCandidate,
    identify_tethering_function,
    parse_lsusb_dump,
)


@dataclass
class ProbeResult:
    """A single result of a probe pass - the device + chosen interface."""

    device: AppleDeviceInfo
    candidate: Optional[TetheringCandidate] = None
    error: str = ""

    @property
    def is_tethering_ready(self) -> bool:
        return self.candidate is not None


# ---------------------------------------------------------------------------
# Linux backend (pure /sys, no root needed)
# ---------------------------------------------------------------------------


def _probe_linux_sysfs() -> List[AppleDeviceInfo]:
    """Walk /sys/bus/usb/devices for Apple composite devices."""
    if not sys.platform.startswith("linux"):
        return []

    base = Path("/sys/bus/usb/devices")
    if not base.exists():
        return []

    devices: List[AppleDeviceInfo] = []
    for entry in sorted(base.iterdir()):
        # Skip root hubs and interfaces (entries like 1-1:1.0)
        if ":" in entry.name:
            continue
        idvendor = entry / "idVendor"
        idproduct = entry / "idProduct"
        if not idvendor.exists() or not idproduct.exists():
            continue
        try:
            vid = int(idvendor.read_text().strip(), 16)
            pid = int(idproduct.read_text().strip(), 16)
        except ValueError:
            continue
        if vid != 0x05AC:
            continue

        interfaces = []
        for iface in sorted(entry.iterdir()):
            if not iface.name.startswith(entry.name + ":"):
                continue
            try:
                with open(iface / "bInterfaceNumber") as f:
                    number = int(f.read().strip())
                with open(iface / "bAlternateSetting") as f:
                    alt = int(f.read().strip())
                with open(iface / "bInterfaceClass") as f:
                    klass = int(f.read().strip(), 16)
                with open(iface / "bInterfaceSubClass") as f:
                    sub = int(f.read().strip(), 16)
                with open(iface / "bInterfaceProtocol") as f:
                    proto = int(f.read().strip(), 16)
            except (FileNotFoundError, ValueError):
                continue

            # Find endpoints and CDC functional descriptors
            has_intr_in = False
            intr_in_ep: Optional[int] = None
            bulk_in_ep: Optional[int] = None
            bulk_out_ep: Optional[int] = None
            union_slave: Optional[int] = None

            for ep in (iface.iterdir() if iface.exists() else []):
                if not ep.name.startswith("ep_"):
                    continue
                epfile = ep / "type"
                if not epfile.exists():
                    continue
                t = epfile.read_text().strip()
                # /sys type string is e.g. "bmAttributes = 02" for bulk
                # or "Bi" / "Bo" abbreviations
                # we use the canonical endpoint descriptor at the kernel's
                # ``endpoint/`` directory which contains ``type`` (decimal),
                # ``direction`` (in/out), ``interval``.
                # Easier: use ``bEndpointAddress`` + ``bmAttributes`` if available.
                addrfile = ep / "bEndpointAddress"
                attrfile = ep / "bmAttributes"
                if not addrfile.exists() or not attrfile.exists():
                    continue
                addr = int(addrfile.read_text().strip(), 16)
                attr = int(attrfile.read_text().strip(), 16)
                direction_in = bool(addr & 0x80)
                xfer = attr & 0x03
                ep_addr = addr & 0x8F
                if xfer == 3:  # interrupt
                    if direction_in:
                        has_intr_in = True
                        intr_in_ep = ep_addr
                elif xfer == 2:  # bulk
                    if direction_in:
                        bulk_in_ep = ep_addr
                    else:
                        bulk_out_ep = ep_addr

            interfaces.append(
                InterfaceDescriptor(
                    number=number,
                    alternate=alt,
                    class_code=klass,
                    subclass=sub,
                    protocol=proto,
                    has_interrupt_in=has_intr_in,
                    interrupt_in_ep=intr_in_ep,
                    bulk_in_ep=bulk_in_ep,
                    bulk_out_ep=bulk_out_ep,
                    cdc_union_slave=union_slave,
                )
            )

        product_string = ""
        manufacturer_string = ""
        serial = ""
        for attr, target in (
            ("product", "product_string"),
            ("manufacturer", "manufacturer_string"),
            ("serial", "serial"),
        ):
            p = entry / attr
            if p.exists():
                try:
                    val = p.read_text().strip()
                    if val:
                        locals()[target] = val
                except OSError:
                    pass

        devices.append(
            AppleDeviceInfo(
                vendor_id=vid,
                product_id=pid,
                bus=None,
                port=None,
                serial=serial,
                product_string=product_string,
                manufacturer_string=manufacturer_string,
                interfaces=tuple(interfaces),
            )
        )

    return devices


# ---------------------------------------------------------------------------
# Windows backend (ctypes against setupapi.dll)
# ---------------------------------------------------------------------------


def _probe_windows_setupapi() -> List[AppleDeviceInfo]:
    """Enumerate USB devices via Windows SetupAPI.

    We call SetupDiGetClassDevs on the USB device setup class, then for each
    devinst fetch its hardware ID and instance path. We never enumerate
    individual interfaces from userspace - that level of detail is enough
    for picking the right devnode to bind a driver to.
    """
    if not sys.platform.startswith("win"):
        return []
    try:
        import ctypes  # noqa: F401  - ctypes is always available on Windows
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - non-Windows
        return []

    setupapi = ctypes.WinDLL("setupapi")
    cfgmgr = ctypes.WinDLL("cfgmgr32")

    GUID_DEVINTERFACE_DISK = b"\x53\xf7\x63\x1d\x10\x5e\x29\x4d\x11\xd2\xb9\x34\x00\xc0\x4f\x79"
    # Use the generic USB device class GUID - same as Device Manager.
    DIGCF_PRESENT = 0x00000002
    DIGCF_DEVICEINTERFACE = 0x00000010

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

    SetupDiDestroyDeviceInfoList = setupapi.SetupDiDestroyDeviceInfoList
    SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    SetupDiDestroyDeviceInfoList.restype = ctypes.c_int

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

    devices: List[AppleDeviceInfo] = []
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
            if "VID_05AC" not in inst.upper():
                continue

            vid, pid = 0x05AC, 0x0000
            mvid = re.search(r"VID_([0-9A-Fa-f]{4})", inst)
            mpid = re.search(r"PID_([0-9A-Fa-f]{4})", inst)
            if mvid:
                vid = int(mvid.group(1), 16)
            if mpid:
                pid = int(mpid.group(1), 16)

            # We don't enumerate per-interface descriptors from userspace here;
            # the binding code (Windows tray app) reads SetupAPI on the MI_xx
            # child devnode to find the CDC-NCM function. For the probe we
            # just mark the device with the bare minimum and let the caller
            # know if it's a known tethering PID.
            devices.append(
                AppleDeviceInfo(
                    vendor_id=vid,
                    product_id=pid,
                    serial="",
                    product_string="",
                    manufacturer_string="",
                    interfaces=(),
                )
            )
    finally:
        SetupDiDestroyDeviceInfoList(handle)

    return devices


# ---------------------------------------------------------------------------
# macOS backend (best-effort, used by dev machines only)
# ---------------------------------------------------------------------------


def _probe_macos_system_profiler() -> List[AppleDeviceInfo]:
    if platform.system() != "Darwin":
        return []
    try:
        out = subprocess.run(
            ["system_profiler", "-xml", "SPUSBDataType", "SPNetworkDataType"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    devices: List[AppleDeviceInfo] = []
    # macOS's XML profile output is too noisy to parse reliably here; we
    # only use it to discover PID/serial strings. Interface discovery is
    # delegated to ``ioreg -p IOUSB`` which we shell out to.
    try:
        ioreg = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-l"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        ioreg = ""

    for block in ioreg.split("+-o "):
        m = re.search(r'"idProduct"\s*=\s*(\S+)', block)
        if not m:
            continue
        pid_hex = m.group(1).rstrip(",")
        try:
            pid = int(pid_hex, 16)
        except ValueError:
            continue
        if pid not in AppleDeviceInfo.KNOWN_TETHERING_PIDS:
            continue
        devices.append(
            AppleDeviceInfo(
                vendor_id=0x05AC,
                product_id=pid,
                interfaces=(),
            )
        )
    return devices


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------


class AppleDeviceProbe:
    """Find an iPhone/iPad on the USB bus and report its tethering state."""

    def __init__(self, prefer_protocol=None) -> None:
        self._prefer = prefer_protocol

    def scan(self) -> List[ProbeResult]:
        """Run a probe pass on the current platform."""
        if sys.platform.startswith("linux"):
            raw = _probe_linux_sysfs()
        elif sys.platform.startswith("win"):
            raw = _probe_windows_setupapi()
        elif sys.platform == "darwin":
            raw = _probe_macos_system_profiler()
        else:
            raw = []

        results: List[ProbeResult] = []
        for info in raw:
            cand = identify_tethering_function(info, self._prefer) if info.interfaces else None
            results.append(ProbeResult(device=info, candidate=cand))
        return results

    def find_first_tethering_ready(self) -> Optional[ProbeResult]:
        for r in self.scan():
            if r.is_tethering_ready:
                return r
        return None


__all__ = ["AppleDeviceProbe", "ProbeResult"]
