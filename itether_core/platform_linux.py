"""
Linux-only helpers used by the tray app and the watchdog.

Everything here shells out to standard binaries that ship with every
Linux distro:

  * ``ip`` from iproute2 (link/IP management)
  * ``dnsmasq`` (DHCP server for the iPhone)
  * ``nft`` / ``iptables`` (NAT for sharing the iPhone link)
  * ``sysfs`` files directly (interface state)
  * ``udevadm`` for hotplug events

We intentionally do not depend on NetworkManager or systemd-resolved.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: List[str], *, check: bool = True, timeout: int = 30, **kw) -> subprocess.CompletedProcess:
    """Run a command capturing stdout/stderr; raise on failure if ``check``."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError as e:
        if check:
            raise RuntimeError(f"command not found: {cmd[0]}") from e
        return subprocess.CompletedProcess(cmd, returncode=errno.ENOENT, stdout="", stderr=str(e))
    except subprocess.TimeoutExpired as e:
        if check:
            raise RuntimeError(f"timeout: {' '.join(cmd)}") from e
        return subprocess.CompletedProcess(cmd, returncode=errno.ETIMEDOUT, stdout="", stderr=str(e))


def ip_link_show(iface: str) -> Tuple[bool, str]:
    """Return ``(is_up, mode_str)`` where mode is one of up/down/unknown."""
    try:
        out = _run(["ip", "-o", "link", "show", iface], check=False).stdout.strip()
    except Exception:
        return False, "unknown"
    if not out:
        return False, "missing"
    return ("state UP" in out), "up" if "state UP" in out else "down"


def ip_link_set(iface: str, *, up: Optional[bool] = None, mtu: Optional[int] = None) -> bool:
    cmds: List[List[str]] = []
    if up is True:
        cmds.append(["ip", "link", "set", iface, "up"])
    elif up is False:
        cmds.append(["ip", "link", "set", iface, "down"])
    if mtu is not None:
        cmds.append(["ip", "link", "set", iface, "mtu", str(mtu)])
    for c in cmds:
        if _run(c, check=False).returncode != 0:
            return False
    return True


def ip_addr_show(iface: str) -> List[Tuple[str, int]]:
    """Return list of ``(address, prefix_len)`` for the given interface."""
    out = _run(["ip", "-4", "-o", "addr", "show", "dev", iface], check=False).stdout
    addrs: List[Tuple[str, int]] = []
    for line in out.splitlines():
        m = re.search(r"inet\s+(\S+)", line)
        if not m:
            continue
        try:
            ip, prefix = m.group(1).split("/", 1)
            addrs.append((ip, int(prefix)))
        except ValueError:
            continue
    return addrs


def ip_addr_add(iface: str, addr: str, prefix: int) -> bool:
    return _run(["ip", "addr", "add", f"{addr}/{prefix}", "dev", iface], check=False).returncode == 0


def ip_addr_flush(iface: str) -> bool:
    return _run(["ip", "addr", "flush", "dev", iface], check=False).returncode == 0


def ip_route_default_via(iface: str) -> Optional[str]:
    out = _run(["ip", "route", "show", "default", "dev", iface], check=False).stdout
    for line in out.splitlines():
        m = re.match(r"default via (\S+)", line)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# DHCP server (dnsmasq)
# ---------------------------------------------------------------------------


@dataclass
class DhcpLease:
    ip: str
    router: str
    dns: str
    range_start: str
    range_end: str
    lease_time_seconds: int = 86400

    def to_dnsmasq(self, iface: str) -> str:
        # dnsmasq config tuned to the iPhone: it expects 172.20.10.x by
        # default but we leave the host in 192.168.x so the iPhone can use
        # any range it advertises via DHCP.
        return (
            f"interface={iface}\n"
            f"bind-interfaces\n"
            f"no-dhcp-interface=lo\n"
            f"dhcp-range={self.range_start},{self.range_end},{self.lease_time_seconds}\n"
            f"dhcp-option=3,{self.router}\n"
            f"dhcp-option=6,{self.dns}\n"
            f"log-queries\n"
            f"log-dhcp\n"
        )


def start_dnsmasq(iface: str, lease: DhcpLease, *, config_dir: Optional[Path] = None) -> Tuple[bool, str]:
    """Start a dnsmasq instance scoped to the tethering interface."""
    cfg_dir = config_dir or Path("/etc/iTether")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "dnsmasq.conf"
    pid_file = cfg_dir / "dnsmasq.pid"
    cfg_file.write_text(lease.to_dnsmasq(iface))
    _run(["pkill", "-f", f"dnsmasq.*itether.*"], check=False)

    log_file = cfg_dir / "dnsmasq.log"
    cmd = [
        "dnsmasq",
        "-C", str(cfg_file),
        "-x", str(pid_file),
        "--log-facility", str(log_file),
    ]
    proc = _run(cmd, check=False)
    if proc.returncode != 0:
        return False, proc.stderr
    return True, f"dnsmasq PID={pid_file}"


def stop_dnsmasq() -> bool:
    return _run(["pkill", "-f", "dnsmasq.*itether"], check=False).returncode == 0


# ---------------------------------------------------------------------------
# NAT / masquerade
# ---------------------------------------------------------------------------


NFT_TABLE = "iTether"
NFT_CHAIN = "masquerade"

NFT_RULES = """
table inet {TABLE} {{
    chain prerouting {{ type nat hook prerouting priority -100; }}
    chain postrouting {{ type nat hook postrouting priority 100; }}
}}
"""

NFT_RULES_WITH_MASQ = """
table inet {TABLE} {{
    chain prerouting {{ type nat hook prerouting priority -100; }}
    chain postrouting {{
        type nat hook postrouting priority 100;
        oifname "{upstream}" masquerade
    }}
}}
"""


def detect_nft() -> bool:
    return shutil.which("nft") is not None


def detect_iptables() -> bool:
    return shutil.which("iptables") is not None


def enable_masquerade(upstream_iface: str) -> Tuple[bool, str]:
    """Set up NAT so traffic from the tethering interface is masqueraded."""
    if detect_nft():
        body = NFT_RULES_WITH_MASQ.format(TABLE=NFT_TABLE, upstream=upstream_iface)
        proc = _run(["nft", "-f", "-"], input=body, check=False)
        return proc.returncode == 0, proc.stderr
    if detect_iptables():
        proc = _run(["iptables", "-t", "nat", "-A", "POSTROUTING",
                     "-o", upstream_iface, "-j", "MASQUERADE"], check=False)
        return proc.returncode == 0, proc.stderr
    return False, "neither nft nor iptables found"


def disable_masquerade(upstream_iface: str) -> bool:
    if detect_nft():
        _run(["nft", "delete", "table", f"inet", NFT_TABLE], check=False)
        return True
    if detect_iptables():
        _run(["iptables", "-t", "nat", "-D", "POSTROUTING",
              "-o", upstream_iface, "-j", "MASQUERADE"], check=False)
        return True
    return False


# ---------------------------------------------------------------------------
# udev: prevent usb_modeswitch / usbmuxd from grabbing our interface
# ---------------------------------------------------------------------------


UDEV_RULES = """
# iTether: keep cdc_ncm/ipheth driver on iPhone tethering function.
# Without this, usbmuxd may claim MI_00 and the tethering function
# never enumerates (see Fedora libimobiledevice discussion).

# Let the kernel enumerate the CDC-NCM function normally
ACTION=="add", SUBSYSTEM=="net", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="12a8", ENV{NM_UNMANAGED}="1"
ACTION=="add", SUBSYSTEM=="net", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="12ab", ENV{NM_UNMANAGED}="1"
ACTION=="add", SUBSYSTEM=="net", ATTRS{idVendor}=="05ac", ATTRS{idProduct}=="1905", ENV{NM_UNMANAGED}="1"
"""


def install_udev_rules(path: str = "/etc/udev/rules.d/99-itether.rules") -> bool:
    try:
        Path(path).write_text(UDEV_RULES)
        _run(["udevadm", "control", "--reload-rules"], check=False)
        return True
    except OSError as e:
        print(f"could not install udev rules: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Watchdog glue
# ---------------------------------------------------------------------------


def get_link_speed(iface: str) -> Optional[int]:
    """Return the link speed in bits per second reported by sysfs, or None."""
    p = Path(f"/sys/class/net/{iface}/speed")
    if not p.exists():
        return None
    try:
        s = int(p.read_text().strip())
    except ValueError:
        return None
    if s <= 0:
        return None
    return s * 1_000_000


def get_carrier_changes(iface: str) -> Optional[int]:
    p = Path(f"/sys/class/net/{iface}/carrier_changes")
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except ValueError:
        return None


def get_link_state(iface: str) -> dict:
    """Return a snapshot suitable for JSON serialisation."""
    up, mode = ip_link_show(iface)
    addrs = ip_addr_show(iface) if up else []
    return {
        "interface": iface,
        "link_up": up,
        "operstate": mode,
        "addresses": [f"{ip}/{prefix}" for ip, prefix in addrs],
        "speed_bps": get_link_speed(iface),
        "carrier_changes": get_carrier_changes(iface),
    }


def diagnose_apple_tether() -> dict:
    """Run a one-shot diagnostic and return a JSON dict."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from .probe import AppleDeviceProbe  # type: ignore

    probe = AppleDeviceProbe()
    results = probe.scan()
    tether = next((r for r in results if r.is_tethering_ready), None)

    iface = ""
    if tether is not None:
        # Try to map the candidate's interface number to a netdev name by
        # scanning /sys/class/net. The kernel exposes cdc_ncm/ipheth devnodes
        # under that directory keyed by name, but the USB bus path is at
        # /sys/class/net/<name>/device -> ../../<bus>-<port>:<cfg>.<iface>
        iface = _find_netdev_for_usb(tether.device.vendor_id, tether.device.product_id, tether.candidate.data_interface) or ""

    return {
        "platform": "linux",
        "tether_devices": [
            {
                "pid": f"0x{r.device.product_id:04X}",
                "serial": r.device.serial,
                "protocol": r.candidate.protocol.value if r.candidate else None,
                "net_interface": iface,
                "link": get_link_state(iface) if iface else None,
            }
            for r in results
            if r.device.is_apple
        ],
    }


def _find_netdev_for_usb(vid: int, pid: int, ifnum: int) -> Optional[str]:
    base = Path("/sys/bus/usb/devices")
    if not base.exists():
        return None
    for entry in base.iterdir():
        if ":" in entry.name:
            continue
        try:
            idvendor = int((entry / "idVendor").read_text().strip(), 16)
            idproduct = int((entry / "idProduct").read_text().strip(), 16)
        except (FileNotFoundError, ValueError):
            continue
        if idvendor != vid or idproduct != pid:
            continue
        # The interface subdirectory is e.g. "1-1:1.4" -> 4 == ifnum
        for sub in entry.iterdir():
            m = re.match(rf".*:(\d+)\.(\d+)", sub.name)
            if not m:
                continue
            cfg, iface_num = int(m.group(1)), int(m.group(2))
            if iface_num == ifnum:
                netdir = sub / "net"
                if netdir.exists():
                    devs = list(netdir.iterdir())
                    if devs:
                        return devs[0].name
    return None


# ---------------------------------------------------------------------------
# systemctl integration
# ---------------------------------------------------------------------------


SYSTEMD_UNIT = """\
[Unit]
Description=iTether background watchdog
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m itether_core.watchdog
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install_systemd_unit() -> bool:
    unit_path = Path("/etc/systemd/system/iTether.service")
    try:
        unit_path.write_text(SYSTEMD_UNIT)
        _run(["systemctl", "daemon-reload"], check=False)
        _run(["systemctl", "enable", "--now", "iTether.service"], check=False)
        return True
    except OSError as e:
        print(f"could not install systemd unit: {e}", file=sys.stderr)
        return False


__all__ = [
    "DhcpLease",
    "ip_link_show",
    "ip_link_set",
    "ip_addr_show",
    "ip_addr_add",
    "ip_addr_flush",
    "ip_route_default_via",
    "start_dnsmasq",
    "stop_dnsmasq",
    "enable_masquerade",
    "disable_masquerade",
    "install_udev_rules",
    "get_link_state",
    "diagnose_apple_tether",
    "install_systemd_unit",
]
