"""
Link-state and throughput monitor.

The monitor is intentionally stateless: callers ask for a :class:`LinkSample`
on demand and the monitor reads ``/sys`` (Linux) or the Network Adapter
counters via the platform wrapper. The UIs run this on a 1 Hz tick.

For Windows the actual counter reads live in :mod:`itether_core.platform_windows`,
which fills in the same :class:`LinkSample` shape via ``Get-NetAdapterStatistics``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional


class LinkState(str, Enum):
    """High-level summary the UI cares about."""

    UNKNOWN = "unknown"
    DISCONNECTED = "disconnected"
    DRIVER_MISSING = "driver_missing"
    CARRIER_DOWN = "carrier_down"
    DHCP_PENDING = "dhcp_pending"
    CONNECTED = "connected"
    SHARING = "sharing"        # host is re-sharing via ICS / masquerade


@dataclass
class LinkSample:
    """One observation of the tethering link."""

    interface_name: str = ""
    state: LinkState = LinkState.UNKNOWN
    timestamp: float = field(default_factory=time.time)
    ipv4_address: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    speed_bps: float = 0.0
    mtu: int = 0
    note: str = ""

    def throughput(self, previous: "LinkSample") -> "LinkSample":
        """Annotate this sample with rx/tx bitrate given a previous one."""
        if previous is None:
            return self
        dt = max(self.timestamp - previous.timestamp, 0.001)
        rx_delta = max(self.rx_bytes - previous.rx_bytes, 0)
        tx_delta = max(self.tx_bytes - previous.tx_bytes, 0)
        self.__dict__["_rx_bps"] = (rx_delta * 8) / dt
        self.__dict__["_tx_bps"] = (tx_delta * 8) / dt
        return self

    @property
    def rx_bps(self) -> float:
        v = getattr(self, "_rx_bps", None)
        return float(v) if v is not None else 0.0

    @property
    def tx_bps(self) -> float:
        v = getattr(self, "_tx_bps", None)
        return float(v) if v is not None else 0.0


class TetheringLinkMonitor:
    """Poll a network interface (named ``iface``) and return :class:`LinkSample`s.

    The platform-specific backends are pluggable: ``linux`` reads sysfs +
    /proc/net/dev, Windows reads NetAdapterCx counters (via the platform
    wrapper). The monitor never opens a socket or sends packets of its own.
    """

    def __init__(
        self,
        iface: str = "",
        sample_provider: Optional[Callable[[str], LinkSample]] = None,
    ) -> None:
        self.iface = iface
        self._provider = sample_provider or _linux_default_provider
        self._previous: Optional[LinkSample] = None
        self._samples: List[LinkSample] = []

    def set_interface(self, iface: str) -> None:
        self.iface = iface
        self._previous = None

    def sample(self) -> LinkSample:
        """Take a fresh sample and update throughput deltas."""
        if not self.iface:
            s = LinkSample(state=LinkState.DISCONNECTED, note="no interface set")
        else:
            s = self._provider(self.iface)
        if self._previous is not None and s.state != LinkState.DISCONNECTED:
            dt = max(s.timestamp - self._previous.timestamp, 0.001)
            rx_delta = max(s.rx_bytes - self._previous.rx_bytes, 0)
            tx_delta = max(s.tx_bytes - self._previous.tx_bytes, 0)
            s.__dict__["_rx_bps"] = (rx_delta * 8) / dt
            s.__dict__["_tx_bps"] = (tx_delta * 8) / dt
        self._previous = s
        self._samples.append(s)
        # keep last 600 samples (10 minutes at 1 Hz)
        if len(self._samples) > 600:
            self._samples = self._samples[-600:]
        return s

    @property
    def history(self) -> List[LinkSample]:
        return list(self._samples)


# ---------------------------------------------------------------------------
# Linux default provider (used when no platform wrapper is injected)
# ---------------------------------------------------------------------------


def _linux_default_provider(iface: str) -> LinkSample:
    s = LinkSample(interface_name=iface)
    if not iface:
        s.state = LinkState.DISCONNECTED
        s.note = "no interface set"
        return s

    sys_path = Path(f"/sys/class/net/{iface}")
    if not sys_path.exists():
        s.state = LinkState.DISCONNECTED
        s.note = "interface not present"
        return s

    try:
        with open(sys_path / "operstate") as f:
            oper = f.read().strip()
        with open(sys_path / "mtu") as f:
            s.mtu = int(f.read().strip())
    except OSError as e:
        s.state = LinkState.DRIVER_MISSING
        s.note = str(e)
        return s

    if oper != "up":
        s.state = LinkState.CARRIER_DOWN
        s.note = f"operstate={oper}"
        return s

    try:
        with open(sys_path / "statistics" / "rx_bytes") as f:
            s.rx_bytes = int(f.read().strip())
        with open(sys_path / "statistics" / "tx_bytes") as f:
            s.tx_bytes = int(f.read().strip())
        with open(sys_path / "statistics" / "rx_packets") as f:
            s.rx_packets = int(f.read().strip())
        with open(sys_path / "statistics" / "tx_packets") as f:
            s.tx_packets = int(f.read().strip())
        with open(sys_path / "statistics" / "rx_errors") as f:
            s.rx_errors = int(f.read().strip())
        with open(sys_path / "statistics" / "tx_errors") as f:
            s.tx_errors = int(f.read().strip())
    except OSError:
        pass

    # Read IPv4 address from /proc/net/fib_trie or addr_assign_type
    try:
        for line in (sys_path / "address").read_text().splitlines():
            pass  # MAC, not useful here
        # Best simple approach: parse `ip -4 addr show <iface>`
        import subprocess
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", iface],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        for tok in out.split():
            if "." in tok and "/" in tok and not tok.startswith(("169", "fe80")):
                s.ipv4_address = tok.split("/")[0]
                break
    except (OSError, subprocess.TimeoutExpired):
        pass

    s.state = LinkState.CONNECTED if s.ipv4_address else LinkState.DHCP_PENDING
    s.speed_bps = 0.0  # would need /sys/class/net/<iface>/speed (USB)
    return s


__all__ = ["LinkState", "LinkSample", "TetheringLinkMonitor"]
