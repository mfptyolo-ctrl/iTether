# iTether troubleshooting

What to do when something goes wrong. Read this end-to-end first; if the
problem persists, file an issue with the output of `iTether-diag` and the
contents of `%ProgramData%\iTether\logs\itether.log` (Windows) or
`journalctl -u iTether` (Linux).

## Decision tree

```
                        ┌────────────────────────┐
                        │ iPhone plugged in?     │
                        │ Personal Hotspot on?   │
                        │ "Trust" tapped?        │
                        └──────────┬─────────────┘
                                   │  yes
                                   ▼
            ┌──────────────────────────────────────────┐
            │ Does the tray icon show "connected"?     │
            └────┬───────────────────────────┬─────────┘
                 │ yes                       │ no
                 ▼                           ▼
       ┌──────────────────────┐     ┌──────────────────────────┐
       │ You're done. Enjoy.  │     │ See "USB not recognized" │
       └──────────────────────┘     │ section below.           │
                                    └──────────────────────────┘
```

## "USB device not recognized"

This is the most common error. It means Windows saw the iPhone
enumerate but couldn't find a driver for the **tethering function**.

**Causes & fixes:**

| Cause | Fix |
|---|---|
| `netaapl64.inf` is missing from DriverStore | Run the iTether installer — it stages `itether_usbncm.inf` which binds `UsbNcm.sys` to the same Apple PIDs. |
| Windows Update replaced `netaapl64.sys` with an incompatible version | Right-click the tray → **Repair connection**. This removes the broken devnode and lets our INF rebind `UsbNcm.sys`. |
| iPhone is locked / on the home screen (not on Personal Hotspot screen) | Unlock the iPhone, open **Settings → Personal Hotspot**, leave it on that screen. |
| iOS 16/17 dual NCM function confusion | The iTether INF binds to all `MI_xx` children of the iPhone composite so it doesn't matter which one Windows picked; PnP will route the right one. If still broken, **Repair connection**. |

## "Connected for 90 s then disconnects"

iOS turns Personal Hotspot off after 90 s of "no clients connected".
iTether's watchdog sends ARP/ICMP keepalives by default. If they're
disabled:

**Windows:**

```powershell
Import-Module "$env:ProgramData\iTether\iTether.psm1" -Force
Get-iTetherStatus | Format-List
```

Verify the watchdog is running:

```powershell
Get-ScheduledTask -TaskName iTether.AutoStart
```

If the task isn't there, re-run `itether_windows\install.ps1`.

**Linux:**

```bash
systemctl status iTether.service
journalctl -u iTether -n 50 --no-pager
```

If the service is failing, check `arping` and `ping` are installed.

## "Adapter says 'No Internet' but I'm tethering"

This is normal — iPhone Personal Hotspot does not provide an HTTP
redirector that Windows recognises. The link is up and traffic flows;
the warning is just misleading. Verify with:

```powershell
ping 8.8.8.8
```

If that works, you have internet.

## Random drops on Linux

| Symptom | Cause | Fix |
|---|---|---|
| No `enx...` device appears when iPhone is plugged in | `usbmuxd` stole the devnode | The udev rule `99-itether.rules` sets `USBMUXD_DISABLE=1`. Confirm it's installed: `cat /etc/udev/rules.d/99-itether.rules`. Run `sudo udevadm control --reload-rules && sudo udevadm trigger`. |
| Interface appears but no DHCP reply | NetworkManager racing for the devnode | Same udev rule sets `NM_UNMANAGED=1`. Or, run `sudo nmcli device set <iface> managed no`. |
| Interface appears, DHCP works, but no internet routing | iPhone hasn't assigned a default route | Check `ip route show dev <iface>` and add one manually: `sudo ip route add default dev <iface>`. |
| Throughput caps at ~10 MB/s | USB 2 bottleneck | Expected for USB 2 tethering. USB-C iPhones on USB 3 hosts see ~250 MB/s. |

## iPhone not detected at all on Linux

```bash
lsusb -d 05ac:
```

If the iPhone isn't listed at all, the cable is bad or the port is
power-only. If it shows up with a different PID, file an issue with the
output — we maintain the known-PID list in
`itether_core/descriptor.py::AppleDeviceInfo::KNOWN_TETHERING_PIDS`.

If it shows up with a known PID but no netdev appears:

```bash
sudo dmesg -w
# plug in the iPhone now
```

Look for `cdc_ncm` or `ipheth` binding messages. If the kernel says
`bind() failure`, the issue is the dual-NCM function (Linux 6.x has the
patch since 6.10; older kernels need an out-of-tree patch).

## Re-shared connection (ICS / masquerade) doesn't reach other devices

**Windows ICS:**

```powershell
Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | Format-Table Name, InterfaceDescription
```

Then re-run:

```powershell
Enable-iTetherInternetSharing -PrivateAdapter "<the LAN adapter name>"
```

Make sure the firewall on the LAN side allows inbound traffic.

**Linux masquerade:**

```bash
sudo nft list table inet iTether
```

If the table is missing, re-enable sharing from the tray, or:

```bash
sudo iTether-diag share <iface>
```

## Filing an issue

Please include:

1. iPhone model + iOS version (Settings → General → About).
2. Windows 10/11 build number (`winver`) or Linux `uname -a`.
3. Output of `iTether-diag` (Windows) or `python3 -m itether_core diag` (Linux).
4. The last 100 lines of `itether.log` (Windows) or `journalctl -u iTether -n 100` (Linux).
5. Whether the device appears in `Device Manager` / `lsusb` *at all*.
