#!/usr/bin/env python3
"""
iTether tray app for Linux (GTK 3).

Requires PyGObject (system package ``python3-gi`` on Debian/Ubuntu,
``python-gobject`` on Arch). The tray icon uses the AppIndicator library
which is widely available as ``gir1.2-appindicator3-*``.

Run with::

    python3 -m itether_linux.itether_tray

or, after installation::

    itether-tray
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Make the package importable when running directly out of the source tree.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from itether_core.probe import AppleDeviceProbe
from itether_core.monitor import TetheringLinkMonitor, LinkState


APPINDICATOR_ID = "itether-tray"
APPINDICATOR_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Indicator
# ---------------------------------------------------------------------------


class iTetherTray:
    def __init__(self) -> None:
        import gi  # type: ignore
        gi.require_version("Gtk", "3.0")
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import Gtk, AppIndicator3, GLib  # type: ignore

        self._Gtk = Gtk
        self._GLib = GLib
        self.indicator = AppIndicator3.Indicator.new(
            APPINDICATOR_ID,
            "network-wireless",
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("iTether")
        self.indicator.set_icon_full("network-wireless", "iTether")

        self._build_menu()

        self._iface: Optional[str] = None
        self._sharing = False
        self._rx_bps = 0.0
        self._tx_bps = 0.0
        self._last_stats = None

        # 1 Hz state poll
        GLib.timeout_add_seconds(1, self._tick)

    # ----- menu -----------------------------------------------------------

    def _build_menu(self) -> None:
        Gtk = self._Gtk

        def make_item(label, tooltip=None, cb=None):
            item = Gtk.MenuItem()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl = Gtk.Label(label=label)
            lbl.set_xalign(0)
            box.pack_start(lbl, True, True, 0)
            item.add(box)
            if tooltip:
                item.set_tooltip_text(tooltip)
            if cb:
                item.connect("activate", cb)
            return item

        menu = Gtk.Menu()

        self._status_item = make_item("iPhone not detected")
        self._status_item.set_sensitive(False)
        menu.append(self._status_item)

        self._throughput_item = make_item("RX 0 b/s   TX 0 b/s")
        self._throughput_item.set_sensitive(False)
        menu.append(self._throughput_item)

        menu.append(Gtk.SeparatorMenuItem())

        share_item = make_item("Share connection to other devices…", cb=self._on_share_clicked)
        menu.append(share_item)

        stop_share_item = make_item("Stop sharing", cb=self._on_stop_share_clicked)
        menu.append(stop_share_item)

        menu.append(Gtk.SeparatorMenuItem())

        repair_item = make_item(
            "Repair connection",
            tooltip="Rebind cdc_ncm, renew DHCP, restart the link",
            cb=self._on_repair_clicked,
        )
        menu.append(repair_item)

        diag_item = make_item("Run diagnostics…", cb=self._on_diag_clicked)
        menu.append(diag_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = make_item("Quit", cb=self._on_quit_clicked)
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    # ----- callbacks ------------------------------------------------------

    def _on_share_clicked(self, *_args) -> None:
        from itether_core.platform_linux import enable_masquerade, DhcpLease, start_dnsmasq
        from itether_core.platform_linux import ip_addr_flush, ip_addr_add

        if not self._iface:
            self._notify("No tethering interface yet")
            return
        try:
            # Use a deterministic address range that's safe and not commonly used.
            ip_addr_flush(self._iface)
            ip_addr_add(self._iface, "192.168.42.1", 24)
            lease = DhcpLease(
                ip="192.168.42.1",
                router="192.168.42.1",
                dns="1.1.1.1,8.8.8.8",
                range_start="192.168.42.10",
                range_end="192.168.42.250",
            )
            ok, msg = start_dnsmasq(self._iface, lease)
            if not ok:
                self._notify("dnsmasq failed: " + msg)
                return
            ok, msg = enable_masquerade(self._iface)
            if not ok:
                self._notify("masquerade failed: " + msg)
                return
            self._sharing = True
            self._notify("Sharing enabled on " + self._iface)
        except Exception as exc:  # noqa: BLE001
            self._notify("Sharing failed: " + str(exc))

    def _on_stop_share_clicked(self, *_args) -> None:
        from itether_core.platform_linux import stop_dnsmasq, disable_masquerade
        stop_dnsmasq()
        if self._iface:
            disable_masquerade(self._iface)
        self._sharing = False
        self._notify("Sharing stopped")

    def _on_repair_clicked(self, *_args) -> None:
        from itether_core.platform_linux import ip_link_set
        if self._iface:
            ip_link_set(self._iface, up=True)
            subprocess.run(["dhclient", "-r", self._iface], check=False)
            subprocess.run(["dhclient", self._iface], check=False)
        self._notify("Repair attempted")

    def _on_diag_clicked(self, *_args) -> None:
        from itether_core.platform_linux import diagnose_apple_tether
        info = diagnose_apple_tether()
        text = json.dumps(info, indent=2)
        self._notify("iTether diagnostics\n" + text[:512])

    def _on_quit_clicked(self, *_args) -> None:
        Gtk = self._Gtk
        Gtk.main_quit()

    # ----- periodic updates ----------------------------------------------

    def _tick(self) -> bool:
        probe = AppleDeviceProbe()
        result = probe.find_first_tethering_ready()
        if result is None:
            self.indicator.set_icon_full("network-offline", "iTether")
            self._status_item.get_child().get_children()[0].set_text("iPhone not detected")
            self._throughput_item.get_child().get_children()[0].set_text("RX 0 b/s   TX 0 b/s")
            self._iface = None
            return True

        from itether_core.platform_linux import _find_netdev_for_usb
        iface = _find_netdev_for_usb(
            result.device.vendor_id,
            result.device.product_id,
            result.candidate.data_interface,
        )
        self._iface = iface
        if iface is None:
            self._status_item.get_child().get_children()[0].set_text("iPhone present, no netdev yet")
            return True

        sample = TetheringLinkMonitor(iface).sample()
        self._rx_bps = sample.rx_bps
        self._tx_bps = sample.tx_bps

        if sample.state == LinkState.CONNECTED:
            self.indicator.set_icon_full("network-wireless-connected-100", "iTether")
            status = f"iPhone tethered on {iface}"
        elif sample.state == LinkState.DHCP_PENDING:
            self.indicator.set_icon_full("network-wireless-acquiring", "iTether")
            status = f"Awaiting DHCP on {iface}"
        elif sample.state in (LinkState.CARRIER_DOWN, LinkState.DISCONNECTED):
            self.indicator.set_icon_full("network-wireless-disconnected", "iTether")
            status = f"{iface}: carrier down"
        else:
            self.indicator.set_icon_full("network-wireless", "iTether")
            status = f"{iface}: {sample.state.value}"

        if self._sharing:
            status += " • SHARING"

        self._status_item.get_child().get_children()[0].set_text(status)
        self._throughput_item.get_child().get_children()[0].set_text(
            f"RX {self._rx_bps/1024.0:0.1f} KiB/s   TX {self._tx_bps/1024.0:0.1f} KiB/s"
        )

        return True

    def _notify(self, message: str) -> None:
        self._Gtk.main_quit  # avoid unused warnings
        try:
            subprocess.Popen(["notify-send", "-a", "iTether", "-i",
                              "network-wireless-connected-100", "iTether", message])
        except FileNotFoundError:
            print(message, file=sys.stderr)


def main() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    tray = iTetherTray()
    try:
        tray._Gtk.main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
