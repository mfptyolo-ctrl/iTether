"""``python3 -m itether_core`` command-line entry point.

Provides ``itether status``, ``itether watch``, ``itether diag``.
"""

import argparse
import json
import sys

from .probe import AppleDeviceProbe
from .monitor import TetheringLinkMonitor


def cmd_status(_args) -> int:
    probe = AppleDeviceProbe()
    results = probe.scan()
    if not results:
        print("No Apple devices detected.")
        return 1
    for r in results:
        print(f"{r.device.manufacturer_string} {r.device.product_string}")
        print(f"  VID:PID = {r.device.vendor_id:04X}:{r.device.product_id:04X}")
        print(f"  serial  = {r.device.serial}")
        if r.candidate is not None:
            print(f"  protocol = {r.candidate.protocol.value}")
            print(f"  control  = {r.candidate.control_interface}")
            print(f"  data     = {r.candidate.data_interface}")
            print(f"  reason   = {r.candidate.reason}")
        else:
            print("  tethering function not yet detected")
        if r.error:
            print(f"  error    = {r.error}")
    return 0


def cmd_watch(_args) -> int:
    from .watchdog import _run
    _run()
    return 0


def cmd_diag(_args) -> int:
    if sys.platform.startswith("win"):
        from .platform_windows import diagnose_apple_tether
        info = diagnose_apple_tether()
    elif sys.platform.startswith("linux"):
        from .platform_linux import diagnose_apple_tether
        info = diagnose_apple_tether()
    else:
        info = {"platform": sys.platform, "supported": False}
    print(json.dumps(info, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="itether",
        description="iPhone USB Personal Hotspot without iTunes / Apple Devices",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show currently attached Apple device(s).").set_defaults(func=cmd_status)
    sub.add_parser("watch", help="Run the tethering watchdog in the foreground.").set_defaults(func=cmd_watch)
    sub.add_parser("diag", help="Dump diagnostic JSON for the host platform.").set_defaults(func=cmd_diag)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
