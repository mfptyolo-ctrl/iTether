"""
iTether core library
====================

Shared, dependency-light library used by both the Windows tray app and the
Linux tray app. Responsible for:

  * Apple iPhone/iPad USB tethering device identification
  * CDC-NCM / ipheth descriptor parsing
  * Personal Hotspot "idle disconnect" detection + mitigation heuristics
  * Throughput and link-state snapshot for the UIs

The library deliberately keeps platform-specific I/O (Windows SetupAPI / PnP,
Linux netlink, libusb) inside thin wrappers so the same dataclasses can be
consumed by either UI.

Everything in this module is pure-Python and tested with the standard library
only. The Windows/Linux wrappers live in :mod:`itether_core.platform_*`.

Public entry points
-------------------

* :class:`AppleDeviceProbe` - locate an attached iPhone over USB
* :class:`TetheringLinkMonitor` - poll link state and stats
* :func:`identify_tethering_function` - parse a live descriptor dump and
  pick the right CDC-NCM or ipheth interface (matches the same logic the
  Apple-mode switch and Microsoft's UsbNcm driver use)
"""

from .descriptor import (
    AppleDeviceInfo,
    InterfaceDescriptor,
    TetheringCandidate,
    TetheringProtocol,
    identify_tethering_function,
    parse_lsusb_dump,
)
from .probe import AppleDeviceProbe, ProbeResult
from .monitor import (
    LinkSample,
    TetheringLinkMonitor,
    LinkState,
)
from .keepalive import KeepaliveStrategy, KeepaliveConfig

__all__ = [
    "AppleDeviceInfo",
    "InterfaceDescriptor",
    "TetheringCandidate",
    "TetheringProtocol",
    "identify_tethering_function",
    "parse_lsusb_dump",
    "AppleDeviceProbe",
    "ProbeResult",
    "LinkSample",
    "TetheringLinkMonitor",
    "LinkState",
    "KeepaliveStrategy",
    "KeepaliveConfig",
]

__version__ = "1.0.0"
