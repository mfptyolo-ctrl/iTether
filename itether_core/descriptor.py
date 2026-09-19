"""
USB descriptor parsing for Apple iPhone/iPad tethering interfaces.

The iPhone exposes a multi-function USB composite device. Depending on the
iOS version and host OS, the *tethering* function can look very different:

================================  ===================  ==========================
iOS version                       Function             Driver we want on the host
================================  ===================  ==========================
iOS 3 - iOS 15 (default mode)     ipheth bulk pair     ipheth (Linux) / Apple
                                                      netaapl64.sys (Windows)
iOS 16+ on macOS                  CDC-NCM (int-EIN)    AppleUSBDeviceNCM / cdc_ncm
iOS 17+ RemoteXPC dual function   CDC-NCM (no int-EIN) cdc_ncm with the apple
                                                      private-interface quirk
================================  ===================  ==========================

The host driver must pick *one* of the NCM-like functions - the second
RemoteXPC function has to be rejected because it would steal the control
endpoint but never carry the DHCP handshake.

The exact rule (matching Apple's behaviour and the Microsoft NetAdapterCx
NCM stack used by ``UsbNcm.sys``) is:

* If both NCM control + NCM data pairs are visible:
  - the tethering function is the one whose CDC **Union descriptor** has
    bMasterInterface = control and bSlaveInterface = data **and**
  - the control function has an **interrupt-IN notification endpoint**
  - the second pair (RemoteXPC) is ignored even if it claims NCM
* If only one function is visible (older iOS / ipheth-only),
  the bulk pair at interface 2 with class 0xFF / subclass 0xFD / protocol
  0x01 is ipheth and is the tethering function

This module implements that exact decision tree in pure Python so the same
heuristic can be reused for the live Windows UI, the Linux UI, and offline
analysis of ``lsusb -vvv`` dumps captured during debugging.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


class TetheringProtocol(str, Enum):
    """How a given Apple function talks to the host."""

    IPHETH = "ipheth"            # legacy iOS 3..15 vendor-specific bulk pair
    CDC_NCM = "cdc_ncm"          # standard CDC-NCM, with int-IN notification
    CDC_NCM_PRIVATE = "cdc_ncm_private"  # iOS 16+ "ncm-control-use-aux"
    CDC_NCM_REMOTEXPC = "remotexpc"      # the second function we reject
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InterfaceDescriptor:
    """Minimal interface descriptor (bInterfaceNumber + class triplet)."""

    number: int
    alternate: int = 0
    class_code: int = 0xFF
    subclass: int = 0x00
    protocol: int = 0x00
    i_interface: str = ""
    has_interrupt_in: bool = False
    interrupt_in_ep: Optional[int] = None  # bEndpointAddress & 0x8F
    bulk_in_ep: Optional[int] = None
    bulk_out_ep: Optional[int] = None
    cdc_union_slave: Optional[int] = None  # for CDC control interfaces
    is_ncm_function: bool = False

    @property
    def is_ipheth_bulk_pair(self) -> bool:
        """ipheth exposes a bulk pair with class 0xFF subclass 0xFD prot 0x01."""
        return (
            self.class_code == 0xFF
            and self.subclass == 0xFD
            and self.protocol == 0x01
            and self.bulk_in_ep is not None
            and self.bulk_out_ep is not None
        )


@dataclass(frozen=True)
class AppleDeviceInfo:
    """Top-level identifying info of an Apple composite device on the bus."""

    vendor_id: int = 0x05AC
    product_id: int = 0x0000
    bus: Optional[int] = None
    port: Optional[int] = None
    serial: str = ""
    product_string: str = ""
    manufacturer_string: str = ""
    interfaces: Tuple[InterfaceDescriptor, ...] = field(default_factory=tuple)

    # Pre-baked "is this an Apple iPhone/iPad we know about?" check.
    # 0x12A8 = iPhone 5..X (Lightning-era tethering)
    # 0x12AB = iPad (Lightning-era tethering)
    # 0x1905 = Apple Silicon Mac over USB-C (shares same NCM protocol)
    KNOWN_TETHERING_PIDS = frozenset({0x12A8, 0x12AB, 0x1905})

    @property
    def is_apple(self) -> bool:
        return self.vendor_id == 0x05AC

    @property
    def is_known_tethering_device(self) -> bool:
        return self.vendor_id == 0x05AC and self.product_id in self.KNOWN_TETHERING_PIDS


@dataclass(frozen=True)
class TetheringCandidate:
    """Result of the analysis: which interface(s) we will use."""

    protocol: TetheringProtocol
    control_interface: int
    data_interface: int
    reason: str = ""
    has_interrupt_endpoint: bool = False
    rejected_remotexpc: bool = False


# ---------------------------------------------------------------------------
# lsusb -vvv parser (used for offline diagnosis and tests)
# ---------------------------------------------------------------------------


_LSUSB_INTERFACE_HEADER = re.compile(
    r"^\s*Interface Descriptor:\s*$"
)
_LSUSB_IFACE_FIELD = re.compile(
    r"^\s*(bInterfaceNumber|bAlternateSetting|bInterfaceClass|bInterfaceSubClass|"
    r"bInterfaceProtocol|iInterface)\s+(.+?)\s*$"
)
_LSUSB_EP_HEADER = re.compile(r"^\s*Endpoint Descriptor:\s*$")
_LSUSB_EP_FIELD = re.compile(
    r"^\s*(bEndpointAddress|bmAttributes|wMaxPacketSize|bInterval)\s+(.+?)\s*$"
)
_LSUSB_UNION = re.compile(
    r"^\s*bSlaveInterface\s+(\d+)\s*$"
)
_LSUSB_DEVICE_HEADER = re.compile(r"^\s*Device Descriptor:\s*$")
_LSUSB_DEVICE_FIELD = re.compile(
    r"^\s*(idVendor|idProduct|iManufacturer|iProduct|iSerial)\s+(.+?)\s*$"
)
_LSUSB_BUS_LINE = re.compile(
    r"^Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})"
)


def parse_lsusb_dump(text: str) -> List[AppleDeviceInfo]:
    """Parse the textual output of ``lsusb -vvv`` into ``AppleDeviceInfo``.

    Non-Apple devices are also returned; the caller can filter on
    :attr:`AppleDeviceInfo.is_apple`.
    """
    devices: List[AppleDeviceInfo] = []
    current_device: Optional[AppleDeviceInfo] = None
    current_interfaces: List[InterfaceDescriptor] = []

    lines = text.splitlines()

    def _is_indented(line: str) -> bool:
        return line.startswith("  ") or line.startswith("\t")

    i = 0
    while i < len(lines):
        line = lines[i]

        # Bus line begins a new device stanza in `lsusb` output.
        m = _LSUSB_BUS_LINE.match(line)
        if m:
            if current_device is not None:
                devices.append(_finalize_device(current_device, current_interfaces))
            current_device = AppleDeviceInfo(
                vendor_id=int(m.group(3), 16),
                product_id=int(m.group(4), 16),
                bus=int(m.group(1)),
                port=int(m.group(2)),
            )
            current_interfaces = []
            i += 1
            continue

        # Device Descriptor (gives strings indexes we may need later)
        if current_device is not None and _LSUSB_DEVICE_HEADER.match(line):
            i += 1
            while i < len(lines) and _is_indented(lines[i]):
                m = _LSUSB_DEVICE_FIELD.match(lines[i])
                if m:
                    key, val = m.group(1), m.group(2)
                    if key == "iManufacturer":
                        current_device = _replace(current_device, manufacturer_string=val)
                    elif key == "iProduct":
                        current_device = _replace(current_device, product_string=val)
                    elif key == "iSerial":
                        current_device = _replace(current_device, serial=val)
                i += 1
            continue

        # Interface Descriptor
        if current_device is not None and _LSUSB_INTERFACE_HEADER.match(line):
            iface_data = {
                "number": 0,
                "alternate": 0,
                "class_code": 0xFF,
                "subclass": 0,
                "protocol": 0,
                "i_interface": "",
                "has_interrupt_in": False,
                "interrupt_in_ep": None,
                "bulk_in_ep": None,
                "bulk_out_ep": None,
                "cdc_union_slave": None,
            }
            i += 1
            # Read interface-level fields. We accept bLength and other
            # descriptor header fields too: they don't change our state
            # but they also don't terminate the block. We stop only when
            # we hit an explicit block terminator (Endpoint Descriptor,
            # CDC functional descriptor, another Interface Descriptor,
            # or a non-indented line).
            while i < len(lines):
                cur = lines[i]
                if not _is_indented(cur):
                    break
                if _LSUSB_INTERFACE_HEADER.match(cur) or _LSUSB_EP_HEADER.match(cur):
                    break
                m = _LSUSB_IFACE_FIELD.match(cur)
                if m:
                    key, val = m.group(1), m.group(2)
                    if key == "bInterfaceNumber":
                        iface_data["number"] = int(val)
                    elif key == "bAlternateSetting":
                        iface_data["alternate"] = int(val)
                    elif key == "bInterfaceClass":
                        iface_data["class_code"] = _to_int(val)
                    elif key == "bInterfaceSubClass":
                        iface_data["subclass"] = _to_int(val)
                    elif key == "bInterfaceProtocol":
                        iface_data["protocol"] = _to_int(val)
                    elif key == "iInterface":
                        iface_data["i_interface"] = val
                # CDC Union / functional descriptors
                mu = _LSUSB_UNION.match(cur)
                if mu:
                    iface_data["cdc_union_slave"] = int(mu.group(1))
                i += 1

            # Now parse any endpoint descriptors that follow.
            while i < len(lines):
                cur = lines[i]
                if not _is_indented(cur):
                    break
                if _LSUSB_INTERFACE_HEADER.match(cur):
                    break
                if _LSUSB_EP_HEADER.match(cur):
                    i += 1
                    ep_dir_in = True
                    ep_xfer_type = 0
                    ep_addr = 0
                    while i < len(lines) and _is_indented(lines[i]):
                        cur2 = lines[i]
                        if _LSUSB_EP_HEADER.match(cur2) or _LSUSB_INTERFACE_HEADER.match(cur2):
                            break
                        m = _LSUSB_EP_FIELD.match(cur2)
                        if m:
                            key, val = m.group(1), m.group(2)
                            if key == "bEndpointAddress":
                                ep_addr = _to_int(val)
                                ep_dir_in = bool(ep_addr & 0x80)
                            elif key == "bmAttributes":
                                ep_xfer_type = _to_int(val) & 0x03
                        i += 1
                    if ep_xfer_type == 3 and ep_dir_in:  # interrupt IN
                        iface_data["has_interrupt_in"] = True
                        iface_data["interrupt_in_ep"] = ep_addr & 0x8F
                    elif ep_xfer_type == 2:  # bulk
                        if ep_dir_in:
                            iface_data["bulk_in_ep"] = ep_addr & 0x8F
                        else:
                            iface_data["bulk_out_ep"] = ep_addr & 0x8F
                    continue
                i += 1

            current_interfaces.append(
                InterfaceDescriptor(
                    number=iface_data["number"],
                    alternate=iface_data["alternate"],
                    class_code=iface_data["class_code"],
                    subclass=iface_data["subclass"],
                    protocol=iface_data["protocol"],
                    i_interface=iface_data["i_interface"],
                    has_interrupt_in=iface_data["has_interrupt_in"],
                    interrupt_in_ep=iface_data["interrupt_in_ep"],
                    bulk_in_ep=iface_data["bulk_in_ep"],
                    bulk_out_ep=iface_data["bulk_out_ep"],
                    cdc_union_slave=iface_data["cdc_union_slave"],
                    is_ncm_function=_looks_like_ncm(
                        iface_data["class_code"],
                        iface_data["subclass"],
                        iface_data["protocol"],
                        iface_data["has_interrupt_in"],
                    ),
                )
            )
            continue

        i += 1

    if current_device is not None:
        devices.append(_finalize_device(current_device, current_interfaces))

    return devices


def _finalize_device(info: AppleDeviceInfo, interfaces: List[InterfaceDescriptor]) -> AppleDeviceInfo:
    return _replace(info, interfaces=tuple(interfaces))


def _replace(info: AppleDeviceInfo, **kwargs) -> AppleDeviceInfo:
    return AppleDeviceInfo(
        vendor_id=kwargs.get("vendor_id", info.vendor_id),
        product_id=kwargs.get("product_id", info.product_id),
        bus=kwargs.get("bus", info.bus),
        port=kwargs.get("port", info.port),
        serial=kwargs.get("serial", info.serial),
        product_string=kwargs.get("product_string", info.product_string),
        manufacturer_string=kwargs.get("manufacturer_string", info.manufacturer_string),
        interfaces=kwargs.get("interfaces", info.interfaces),
    )


def _to_int(raw: str) -> int:
    """Best-effort parse of an lsusb hex/dec numeric field.

    ``raw`` may contain trailing annotation text after the actual numeric
    value (``"0x86  EP 6 IN"`` for an endpoint address, ``"1x 16 bytes"``
    for wMaxPacketSize).

    lsusb prints decimal values without a prefix and hex values with the
    ``0x`` prefix - we honour that explicitly rather than guessing.
    """
    raw = raw.strip()
    if not raw:
        return 0
    head = raw.split()[0]
    if head.startswith(("0x", "0X")):
        try:
            return int(head, 16)
        except ValueError:
            return 0
    try:
        return int(head)
    except ValueError:
        return 0


def _looks_like_ncm(class_code: int, subclass: int, protocol: int, has_intr_in: bool) -> bool:
    """CDC Communications class with NCM subclass 0x0D (or private 0x0E)."""
    if class_code == 0x02 and subclass in (0x0D, 0x0E):
        return True
    # Apple private "ncm-control-use-aux" sometimes uses class 0xFF/0xFD
    if class_code == 0xFF and subclass == 0xFD and has_intr_in:
        return True
    return False


# ---------------------------------------------------------------------------
# Decision tree
# ---------------------------------------------------------------------------


def identify_tethering_function(
    info: AppleDeviceInfo,
    prefer_protocol: Sequence[TetheringProtocol] = (
        TetheringProtocol.CDC_NCM,
        TetheringProtocol.CDC_NCM_PRIVATE,
        TetheringProtocol.IPHETH,
    ),
) -> Optional[TetheringCandidate]:
    """Pick the single tethering function we want to bind a driver to.

    ``prefer_protocol`` controls the order in which we consider protocols.
    The default prefers CDC-NCM (modern iOS), then falls back to the legacy
    ipheth bulk pair.
    """
    if not info.is_apple:
        return None

    by_number = {iface.number: iface for iface in info.interfaces}

    # ----- 1. Look for CDC-NCM control/data pairs -----
    ncm_control: List[InterfaceDescriptor] = []
    for iface in info.interfaces:
        # Standard CDC NCM: class 0x02 subclass 0x0D (or 0x0E for NCM-ECM)
        if iface.class_code == 0x02 and iface.subclass in (0x0D, 0x0E):
            ncm_control.append(iface)
        # Apple private NCM: class 0xFF subclass 0xFD with int-IN endpoint.
        # Marked as NCM by ``_looks_like_ncm``.
        elif iface.is_ncm_function and iface.class_code == 0xFF and iface.subclass == 0xFD:
            ncm_control.append(iface)

    if ncm_control:
        # Prefer the one with the interrupt-IN notification endpoint
        with_intr = [i for i in ncm_control if i.has_interrupt_in]
        candidates = with_intr if with_intr else ncm_control

        # Among candidates, prefer CDC subclass 0x0D (standard NCM)
        candidates.sort(key=lambda i: (i.subclass != 0x0D, i.class_code != 0x02, i.number))

        chosen = candidates[0]
        slave = chosen.cdc_union_slave
        if slave is None or slave not in by_number:
            return TetheringCandidate(
                protocol=TetheringProtocol.CDC_NCM_PRIVATE if not chosen.has_interrupt_in else TetheringProtocol.CDC_NCM,
                control_interface=chosen.number,
                data_interface=slave if slave is not None else chosen.number + 1,
                reason="cdc-ncm without union descriptor; using +1 heuristic",
                has_interrupt_endpoint=chosen.has_interrupt_in,
                rejected_remotexpc=len(ncm_control) > 1,
            )

        rejected = len(ncm_control) > 1
        return TetheringCandidate(
            protocol=(
                TetheringProtocol.CDC_NCM if chosen.has_interrupt_in else TetheringProtocol.CDC_NCM_PRIVATE
            ),
            control_interface=chosen.number,
            data_interface=slave,
            reason=(
                "interrupt-bearing NCM function selected"
                if chosen.has_interrupt_in
                else "NCM without interrupt endpoint; treated as private"
            ),
            has_interrupt_endpoint=chosen.has_interrupt_in,
            rejected_remotexpc=rejected,
        )

    # ----- 2. Legacy ipheth: bulk pair, class 0xFF subclass 0xFD prot 0x01 -----
    for iface in info.interfaces:
        if iface.is_ipheth_bulk_pair:
            return TetheringCandidate(
                protocol=TetheringProtocol.IPHETH,
                control_interface=iface.number,
                data_interface=iface.number,
                reason="ipheth bulk pair found",
                has_interrupt_endpoint=False,
            )

    return None


__all__ = [
    "AppleDeviceInfo",
    "InterfaceDescriptor",
    "TetheringProtocol",
    "TetheringCandidate",
    "parse_lsusb_dump",
    "identify_tethering_function",
]
