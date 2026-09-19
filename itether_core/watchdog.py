"""
Background watchdog: periodic tethering link health checks.

Behaviour:

1.  Watch for the iPhone tethering interface to appear (hotplug).
2.  When it appears:
      * Bring the link up (if the kernel left it down)
      * Configure an IP address on the link if the iPhone DHCP'd us a
        default route via link-local. (Most Linux distros do this via
        NetworkManager or systemd-networkd; we don't assume either.)
      * Start a tiny ARP/ICMP keepalive so iOS doesn't put the hotspot
        to sleep after 90 s of "no clients connected".
3.  When the link drops, tear down the keepalive thread and stop the
    DHCP server so we don't leave stale state.

The watchdog is platform-aware: on Linux it uses :mod:`platform_linux`,
on Windows the PowerShell module is invoked.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .keepalive import KeepaliveConfig, KeepaliveStrategy
from .probe import AppleDeviceProbe
from .monitor import TetheringLinkMonitor, LinkState

# Add parent dir to sys.path so we can `import platform_linux` etc.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _run() -> None:
    cfg = KeepaliveConfig()
    probe = AppleDeviceProbe()

    stop_event = threading.Event()

    def _on_signal(*_):
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    keepalive_thread: Optional[threading.Thread] = None
    iface_under_watch: Optional[str] = None

    print("[iTether watchdog] started")
    while not stop_event.is_set():
        try:
            result = probe.find_first_tethering_ready()
            if result is None:
                time.sleep(5)
                continue

            iface = _resolve_iface(result)
            if iface is None:
                # We know we have a USB device but Linux hasn't given us a
                # netdev yet (the kernel module may not be loaded). Try
                # modprobe.
                _ensure_kernel_module(result.candidate.protocol.value)
                time.sleep(2)
                continue

            if iface != iface_under_watch:
                print(f"[iTether watchdog] tethering interface: {iface}")
                iface_under_watch = iface
                if keepalive_thread is not None:
                    stop_event.set()  # signal the old thread
                    keepalive_thread.join(timeout=3)
                    stop_event = threading.Event()
                    _on_signal = lambda *_: stop_event.set()  # noqa: E731
                    signal.signal(signal.SIGTERM, _on_signal)
                    signal.signal(signal.SIGINT, _on_signal)

                keepalive_thread = threading.Thread(
                    target=_keepalive_loop,
                    args=(iface, cfg, stop_event),
                    daemon=True,
                )
                keepalive_thread.start()

            time.sleep(5)
        except Exception as exc:  # noqa: BLE001 - log and keep running
            print(f"[iTether watchdog] error: {exc!r}", file=sys.stderr)
            time.sleep(5)


def _resolve_iface(result) -> Optional[str]:
    """Resolve the netdev name for the tethering candidate on Linux."""
    if not sys.platform.startswith("linux"):
        return None
    from .platform_linux import _find_netdev_for_usb  # type: ignore

    if result.candidate is None:
        return None
    return _find_netdev_for_usb(
        result.device.vendor_id,
        result.device.product_id,
        result.candidate.data_interface,
    )


def _ensure_kernel_module(protocol: str) -> None:
    if not sys.platform.startswith("linux"):
        return
    if protocol == "ipheth":
        os.system("modprobe ipheth 2>/dev/null")
    elif protocol in ("cdc_ncm", "cdc_ncm_private"):
        os.system("modprobe cdc_ncm cdc_ether 2>/dev/null")


def _keepalive_loop(iface: str, cfg: KeepaliveConfig, stop: threading.Event) -> None:
    """Send periodic keepalives to the iPhone."""
    monitor = TetheringLinkMonitor(iface)
    last_keepalive = 0.0
    while not stop.is_set():
        try:
            sample = monitor.sample()
            now = time.time()

            # If the link went down, record a rebind attempt.
            if sample.state in (LinkState.DISCONNECTED, LinkState.DRIVER_MISSING):
                cfg.record_rebind()
                # Try to bring the interface back up; the kernel may have
                # left it down because of a PnP glitch.
                from .platform_linux import ip_link_set
                ip_link_set(iface, up=True)
                time.sleep(2)
                continue

            if now - last_keepalive >= cfg.interval_seconds:
                _send_keepalive(iface, cfg)
                last_keepalive = now
            time.sleep(1)
        except Exception as exc:  # noqa: BLE001
            print(f"[iTether keepalive] error: {exc!r}", file=sys.stderr)
            time.sleep(2)


def _send_keepalive(iface: str, cfg: KeepaliveConfig) -> None:
    """Run the keepalive strategies the user enabled."""
    if not sys.platform.startswith("linux"):
        return
    from .platform_linux import ip_route_default_via

    if cfg.enabled & KeepaliveStrategy.ARP_KEEPALIVE:
        gw = ip_route_default_via(iface)
        if gw:
            # `arping` is part of iputils on most distros.
            os.system(f"arping -c 1 -U -I {iface} {gw} >/dev/null 2>&1")

    if cfg.enabled & KeepaliveStrategy.ICMP_PING:
        gw = ip_route_default_via(iface)
        if gw:
            os.system(f"ping -c 1 -W 2 {gw} >/dev/null 2>&1")


def main() -> None:
    try:
        _run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
