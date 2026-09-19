"""Unit tests for the keepalive + monitor glue."""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from itether_core.keepalive import KeepaliveConfig, KeepaliveStrategy
from itether_core.monitor import LinkState, LinkSample, TetheringLinkMonitor


class KeepaliveTests(unittest.TestCase):
    def test_default_strategies(self) -> None:
        cfg = KeepaliveConfig()
        # The watchdog enables everything sensible by default.
        self.assertTrue(cfg.enabled & KeepaliveStrategy.ARP_KEEPALIVE)
        self.assertTrue(cfg.enabled & KeepaliveStrategy.ICMP_PING)
        self.assertTrue(cfg.enabled & KeepaliveStrategy.TCP_FTP_KEEPALIVE)
        self.assertTrue(cfg.enabled & KeepaliveStrategy.AUTO_REBIND)

    def test_record_rebind_keeps_history_window(self) -> None:
        cfg = KeepaliveConfig()
        cfg.record_rebind()
        cfg.record_rebind()
        self.assertEqual(len(cfg.rebind_history), 2)
        self.assertIsNotNone(cfg.last_rebind_at)
        self.assertEqual(cfg.rebinds_last_hour, 2)

    def test_combine_flags(self) -> None:
        combined = KeepaliveStrategy.ARP_KEEPALIVE | KeepaliveStrategy.ICMP_PING
        self.assertTrue(combined & KeepaliveStrategy.ARP_KEEPALIVE)
        self.assertTrue(combined & KeepaliveStrategy.ICMP_PING)
        self.assertFalse(combined & KeepaliveStrategy.AUTO_REBIND)


class MonitorTests(unittest.TestCase):
    def test_sample_with_no_interface_is_disconnected(self) -> None:
        m = TetheringLinkMonitor()
        s = m.sample()
        self.assertEqual(s.state, LinkState.DISCONNECTED)
        self.assertEqual(s.interface_name, "")

    def test_throughput_calculation(self) -> None:
        # Two samples - second one reports deltas.
        m = TetheringLinkMonitor(iface="test")
        # First sample: no previous, so no deltas computed
        first = LinkSample(
            interface_name="test",
            rx_bytes=1000,
            tx_bytes=500,
            timestamp=1.0,
        )
        m._provider = lambda _name: first
        s0 = m.sample()
        self.assertEqual(s0.rx_bps, 0.0)

        # Second sample 1 second later: 1000 rx bytes and 1000 tx bytes more
        second = LinkSample(
            interface_name="test",
            rx_bytes=2000,
            tx_bytes=1500,
            timestamp=2.0,
        )
        m._provider = lambda _name: second
        s = m.sample()
        self.assertAlmostEqual(s.rx_bps, 8000.0)
        self.assertAlmostEqual(s.tx_bps, 8000.0)


if __name__ == "__main__":
    unittest.main()
