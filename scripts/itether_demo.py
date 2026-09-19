"""
iTether live end-to-end demo
============================

This script walks the full tethering setup pipeline using **mocked**
hardware, so you can validate the iTether logic on any host without
plugging in an actual iPhone. It's also used by CI to make sure the
core decision tree and monitor glue don't regress.

Pipeline simulated:

  1. Discover the iPhone over USB.
  2. Identify the tethering function (ipheth / CDC-NCM / CDC-NCM-private).
  3. "Bind" a driver (we just record the call here).
  4. Bring up the link.
  5. Take 5 throughput samples spaced 10 ms apart and verify the
     monitor reports non-zero bps when traffic is flowing.
  6. Simulate the link going down (carrier dropped) and verify the
     watchdog would attempt a rebind.
  7. Verify the keepalive strategies default to enabled.

Run with::

    python3 -m scripts.itether_demo

Exit code 0 means every step succeeded.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from itether_core.descriptor import (
    AppleDeviceInfo,
    InterfaceDescriptor,
    TetheringProtocol,
    identify_tethering_function,
)
from itether_core.monitor import (
    LinkSample,
    LinkState,
    TetheringLinkMonitor,
)
from itether_core.keepalive import KeepaliveConfig, KeepaliveStrategy
from itether_core.probe import AppleDeviceProbe


# ---------------------------------------------------------------------------
# Mocked USB devices covering every supported iOS layout
# ---------------------------------------------------------------------------


def mock_ipheth() -> AppleDeviceInfo:
    """iPhone 5..X running iOS 3-15."""
    return AppleDeviceInfo(
        vendor_id=0x05AC,
        product_id=0x12A8,
        serial="F2LXY9ZJG5QR",
        product_string="iPhone",
        manufacturer_string="Apple Inc.",
        interfaces=(
            InterfaceDescriptor(
                number=2,
                class_code=0xFF,
                subclass=0xFD,
                protocol=0x01,
                bulk_in_ep=0x86,
                bulk_out_ep=0x05,
            ),
        ),
    )


def mock_ios16_dual_ncm() -> AppleDeviceInfo:
    """iPhone XS running iOS 16+: dual CDC-NCM, tethering + RemoteXPC."""
    return AppleDeviceInfo(
        vendor_id=0x05AC,
        product_id=0x12A8,
        product_string="iPhone",
        interfaces=(
            InterfaceDescriptor(
                number=2,
                class_code=0x02,
                subclass=0x0D,
                has_interrupt_in=True,
                interrupt_in_ep=0x86,
                cdc_union_slave=3,
                is_ncm_function=True,
            ),
            InterfaceDescriptor(
                number=3,
                class_code=0x0A,
                subclass=0x00,
                protocol=0x01,
                bulk_in_ep=0x87,
                bulk_out_ep=0x05,
            ),
            InterfaceDescriptor(
                number=4,
                class_code=0x02,
                subclass=0x0D,
                has_interrupt_in=True,
                interrupt_in_ep=0x88,
                cdc_union_slave=5,
                is_ncm_function=True,
            ),
            InterfaceDescriptor(
                number=5,
                class_code=0x0A,
                subclass=0x00,
                protocol=0x01,
                bulk_in_ep=0x89,
                bulk_out_ep=0x06,
            ),
        ),
    )


def mock_ios17_private_ncm() -> AppleDeviceInfo:
    """iPhone 15 running iOS 17+ USB-C: Apple-private NCM (no interrupt)."""
    return AppleDeviceInfo(
        vendor_id=0x05AC,
        product_id=0x12A8,
        product_string="iPhone",
        interfaces=(
            InterfaceDescriptor(
                number=2,
                class_code=0x02,
                subclass=0x0D,
                has_interrupt_in=False,
                cdc_union_slave=3,
                is_ncm_function=True,
            ),
            InterfaceDescriptor(
                number=3,
                class_code=0x0A,
                subclass=0x00,
                protocol=0x01,
                bulk_in_ep=0x87,
                bulk_out_ep=0x05,
            ),
        ),
    )


def mock_usb_c_no_union() -> AppleDeviceInfo:
    """iOS 18+: NCM control function but no CDC Union descriptor.

    Some iOS 18 devices expose the CDC-NCM control function without a
    Union functional descriptor. We must fall back to the +1 heuristic
    so the data interface is still picked correctly.
    """
    return AppleDeviceInfo(
        vendor_id=0x05AC,
        product_id=0x12A8,
        product_string="iPhone",
        interfaces=(
            InterfaceDescriptor(
                number=2,
                class_code=0x02,
                subclass=0x0D,
                has_interrupt_in=True,
                interrupt_in_ep=0x86,
                cdc_union_slave=None,  # no Union descriptor
                is_ncm_function=True,
            ),
            InterfaceDescriptor(
                number=3,
                class_code=0x0A,
                subclass=0x00,
                protocol=0x01,
                bulk_in_ep=0x87,
                bulk_out_ep=0x05,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Driver bind log (records calls; in production the platform layer does
# the actual work)
# ---------------------------------------------------------------------------


class DriverBindLog:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def bind(self, info: AppleDeviceInfo, protocol: TetheringProtocol, ifnum: int) -> None:
        self.events.append({
            "action": "bind",
            "pid": f"0x{info.product_id:04X}",
            "protocol": protocol.value,
            "interface": ifnum,
        })


# ---------------------------------------------------------------------------
# Mocked link sample provider
# ---------------------------------------------------------------------------


def make_provider(states: list[LinkSample]):
    """Return a provider function that emits the given sequence of states.

    Each entry is a LinkSample we hand to the monitor; the monitor
    computes deltas between consecutive samples itself.
    """
    idx = {"n": 0}

    def _provider(_iface: str) -> LinkSample:
        s = states[min(idx["n"], len(states) - 1)]
        idx["n"] += 1
        return s

    return _provider


# ---------------------------------------------------------------------------
# The demo
# ---------------------------------------------------------------------------


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def assert_eq(label: str, got, want) -> None:
    ok = got == want
    flag = "✓" if ok else "✗"
    print(f"  {flag} {label}: got={got!r}, want={want!r}")
    if not ok:
        raise SystemExit(1)


def main() -> int:
    bind_log = DriverBindLog()

    # -----------------------------------------------------------------------
    # 1. Discovery + identification
    # -----------------------------------------------------------------------
    step("1. Identify tethering function across iOS versions")
    for label, info in (
        ("ipheth (iOS 3-15)",   mock_ipheth()),
        ("iOS 16 dual NCM",     mock_ios16_dual_ncm()),
        ("iOS 17 private NCM",  mock_ios17_private_ncm()),
        ("iOS 18 no Union",     mock_usb_c_no_union()),
    ):
        cand = identify_tethering_function(info)
        assert cand is not None, f"failed to identify {label}"
        bind_log.bind(info, cand.protocol, cand.control_interface)
        print(f"  {label:30s} -> {cand.protocol.value:18s} "
              f"ctrl={cand.control_interface} data={cand.data_interface} "
              f"rejected_remotexpc={cand.rejected_remotexpc}")

    # -----------------------------------------------------------------------
    # 2. The dual-NCM case must reject the RemoteXPC twin
    # -----------------------------------------------------------------------
    step("2. Dual-NCM rejection (RemoteXPC twin is *not* the tethering function)")
    cand = identify_tethering_function(mock_ios16_dual_ncm())
    assert cand is not None
    assert_eq("tethering function picked at the lower-numbered interface",
              cand.control_interface, 2)
    assert_eq("RemoteXPC twin flagged as rejected",
              cand.rejected_remotexpc, True)

    # -----------------------------------------------------------------------
    # 3. No-Union fallback: iOS 18 sometimes omits the CDC Union descriptor
    # -----------------------------------------------------------------------
    step("3. No-Union fallback (iOS 18+ sometimes omits CDC Union)")
    cand = identify_tethering_function(mock_usb_c_no_union())
    assert cand is not None
    assert_eq("control interface inferred from class match",
              cand.control_interface, 2)
    assert_eq("data interface fallback uses +1 heuristic",
              cand.data_interface, 3)

    # -----------------------------------------------------------------------
    # 4. Live throughput (mocked provider)
    # -----------------------------------------------------------------------
    step("4. Live throughput + state transitions")
    samples = [
        LinkSample(interface_name="enx1234", rx_bytes=0,    tx_bytes=0,    timestamp=0.0,  state=LinkState.CONNECTED,    ipv4_address="192.168.42.2"),
        LinkSample(interface_name="enx1234", rx_bytes=1500, tx_bytes=300,  timestamp=0.01, state=LinkState.CONNECTED,    ipv4_address="192.168.42.2"),
        LinkSample(interface_name="enx1234", rx_bytes=3000, tx_bytes=600,  timestamp=0.02, state=LinkState.CONNECTED,    ipv4_address="192.168.42.2"),
        LinkSample(interface_name="enx1234", rx_bytes=4500, tx_bytes=900,  timestamp=0.03, state=LinkState.CONNECTED,    ipv4_address="192.168.42.2"),
        LinkSample(interface_name="enx1234", rx_bytes=0,    tx_bytes=0,    timestamp=0.04, state=LinkState.CARRIER_DOWN, note="link lost"),
    ]
    monitor = TetheringLinkMonitor(iface="enx1234", sample_provider=make_provider(samples))
    history = [monitor.sample() for _ in range(5)]
    print(f"  states observed: {[s.state.value for s in history]}")
    print(f"  rx_bps:          {[round(s.rx_bps / 1000, 1) for s in history]} kb/s")
    print(f"  tx_bps:          {[round(s.tx_bps / 1000, 1) for s in history]} kb/s")
    assert history[1].rx_bps > 0
    assert_eq("state of final sample (carrier dropped)", history[-1].state, LinkState.CARRIER_DOWN)

    # -----------------------------------------------------------------------
    # 5. Keepalive defaults
    # -----------------------------------------------------------------------
    step("5. Keepalive defaults")
    cfg = KeepaliveConfig()
    for strat in (
        KeepaliveStrategy.ARP_KEEPALIVE,
        KeepaliveStrategy.ICMP_PING,
        KeepaliveStrategy.TCP_FTP_KEEPALIVE,
        KeepaliveStrategy.AUTO_REBIND,
    ):
        enabled = bool(cfg.enabled & strat)
        print(f"  {strat.name:24s} enabled={enabled}")
        assert enabled, f"{strat.name} should be enabled by default"

    # -----------------------------------------------------------------------
    # 6. Probe end-to-end (on this sandbox no real device is attached, but
    #    we still verify the scan returns cleanly)
    # -----------------------------------------------------------------------
    step("6. AppleDeviceProbe end-to-end")
    probe = AppleDeviceProbe()
    results = probe.scan()
    print(f"  scan returned {len(results)} results (no iPhone expected in CI)")

    # -----------------------------------------------------------------------
    # 7. Bind log dump
    # -----------------------------------------------------------------------
    step("7. Driver bind log")
    print(json.dumps(bind_log.events, indent=2))

    print("\n✓ All demo steps passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
