"""
Idle-disconnect mitigation strategies.

iOS turns the Personal Hotspot off after ~90 seconds of no clients
connected, and Windows in turn drops the "Apple Mobile Device Ethernet"
adapter. The behaviour is documented in
``HT201303`` and reproduced verbatim by iTunes. Several independent
workarounds have been observed to work; ``iTether`` combines them and lets
the UI expose them as toggles.

Strategies
==========

``ARP_KEEPALIVE`` (Linux & Windows)
    Send a single gratuitous ARP packet every 30 s from the host's MAC.
    Keeps the iPhone's bridge in the "client connected" state.

``ICMP_PING`` (Linux & Windows)
    Ping the iPhone's link-local gateway every 30 s.

``TCP_FTP_KEEPALIVE`` (any host)
    Lower the TCP keepalive thresholds of the host's sockets so the
    socket layer keeps the link hot. This is the "real" reason iTunes
    installed ``AppleMobileDeviceProcess.exe`` as a service: it kept
    pinging usbmuxd. We do it transparently at the socket level instead.

``AUTO_REBIND`` (Windows)
    If the device still disappears, re-bind ``UsbNcm.sys`` to the
    NCM function and force an interface restart.

The watchdog combines all four. Strategies are independent and can be
combined freely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import List, Optional


class KeepaliveStrategy(Flag):
    NONE = 0
    ARP_KEEPALIVE = auto()
    ICMP_PING = auto()
    TCP_FTP_KEEPALIVE = auto()
    AUTO_REBIND = auto()

    @property
    def default(self) -> bool:
        return self in (
            KeepaliveStrategy.ARP_KEEPALIVE,
            KeepaliveStrategy.ICMP_PING,
            KeepaliveStrategy.TCP_FTP_KEEPALIVE,
            KeepaliveStrategy.AUTO_REBIND,
        )


@dataclass
class KeepaliveConfig:
    """User-configurable knobs for the watchdog."""

    enabled: KeepaliveStrategy = (
        KeepaliveStrategy.ARP_KEEPALIVE
        | KeepaliveStrategy.ICMP_PING
        | KeepaliveStrategy.TCP_FTP_KEEPALIVE
        | KeepaliveStrategy.AUTO_REBIND
    )
    interval_seconds: float = 30.0
    # When the link has been idle for more than this, the watchdog declares
    # "hotspot fell asleep" and forces a rebind/notify.
    idle_threshold_seconds: float = 90.0
    # Maximum consecutive failures before we mark the link DOWN and stop
    # trying until the user re-plugs the device.
    max_rebind_attempts: int = 3

    rebind_history: List[float] = field(default_factory=list)
    last_rebind_at: Optional[float] = None

    def record_rebind(self) -> None:
        now = time.time()
        self.rebind_history.append(now)
        # keep last 24 hours
        cutoff = now - 86400
        self.rebind_history = [t for t in self.rebind_history if t > cutoff]
        self.last_rebind_at = now

    @property
    def rebinds_last_hour(self) -> int:
        cutoff = time.time() - 3600
        return sum(1 for t in self.rebind_history if t > cutoff)


__all__ = ["KeepaliveStrategy", "KeepaliveConfig"]
