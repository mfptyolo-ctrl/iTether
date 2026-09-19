# iTether — Personal Hotspot over USB without iTunes / Apple Devices

`iTether` is a small, cross-platform tool that replaces `iTunes` (and the
newer `Apple Devices` app) **for the single feature most people actually
need it for**: sharing an iPhone or iPad's Personal Hotspot with the host
computer over USB at full speed, without random disconnects, and without
the dreaded "USB device has an error and cannot be recognized" error
Windows shows when iTunes is missing or broken.

It also lets you re-share that link over Ethernet/Wi-Fi to other devices
on your LAN using the OS's built-in sharing (Windows ICS / Linux
nftables masquerade).

```
        +---------------------+
        | iPhone (Hotspot)    |
        |  CDC-NCM tethering  |
        +----------+----------+
                   |  USB
                   v
        +----------+----------+
        |  host PC: iTether   |
        |  (Win tray / Linux  |
        |   AppIndicator)     |
        +----------+----------+
                   |
         +---------+---------+
         v                   v
       host              re-shared to
       routes            Wi-Fi/Ethernet
       through           via ICS / nft
       iPhone
```

## What iTunes does that we replicate

| What iTunes does | iTether equivalent |
|---|---|
| Ships `netaapl64.sys` + `netaapl64.inf` matched to Apple PIDs and binds it on plug-in | Ships `itether_usbncm.inf` that binds **Microsoft in-box `UsbNcm.sys`** to the same Apple PIDs — no third-party `.sys`, no signing woes |
| Auto-launches `AppleMobileDeviceProcess.exe` to keep the link alive | Background `watchdog` + tray app sends keepalive (ARP / ICMP / driver rebind) so iOS does not turn Personal Hotspot off after 90 s of inactivity |
| Sets "Maximum Performance" on the Apple adapter via its kernel-mode driver | Same: PowerShell `Set-NetAdapterAdvancedProperty` + Linux `ip link set ... up` on plug-in |
| Enables Internet Connection Sharing for the Apple adapter when the user ticks the box | One-click "Share connection to other devices…" in the tray app, or `Enable-iTetherInternetSharing` PowerShell cmdlet / `Enable-iTetherInternetSharing` Python equivalent |
| Re-installs the driver if Windows Update strips it | `Repair-iTetherConnection` PowerShell cmdlet — kill stale filters, rebind, restart adapter, renew DHCP |

## Why the "USB device not recognized" error happens

When you plug an iPhone into Windows, the iPhone enumerates a multi-function
USB composite device. Windows needs a driver for the **tethering function**
(the CDC-NCM interface that ships IP traffic). The driver Apple ships,
`netaapl64.sys`, is bundled in iTunes (and in the standalone `Apple Devices`
app from the Microsoft Store).

Several things go wrong:

1. **`iTunes` was uninstalled** → driver deleted from DriverStore.
2. **`Windows Update`** can replace `netaapl64.sys` with an incompatible
   update; the devnode ends up with problem code `CM_PROB_DRIVER_FAILED`.
3. **`iPhone in lock screen`** → iPhone presents *different* USB
   configurations, one of which exposes the tethering function and others
   which do not.
4. **iOS 16/17 dual NCM function** — Apple now exposes two CDC-NCM
   functions (the second one is for `RemoteXPC`). If the wrong one is
   bound, the link never comes up.

iTether solves all four by:

* Binding `UsbNcm.sys` to the **interrupt-bearing** CDC-NCM function
  only (matching Apple's own `AppleUSBDeviceNCMControl` selection rule).
* Surfacing the broken devnode via SetupAPI + `CM_Get_DevNode_Status` so
  the UI can warn the user and offer to repair.
* Re-installing the driver on a single click of **Repair connection** in
  the tray.

## Project layout

```
itether/
├── itether_core/           # Cross-platform Python library (USB parsing, monitor, keepalive)
│   ├── descriptor.py       # lsusb -vvv parser + tethering function decision tree
│   ├── probe.py            # Live USB enumeration (Linux /sys, Windows SetupAPI, macOS ioreg)
│   ├── monitor.py          # Link-state + throughput monitor
│   ├── keepalive.py        # Idle-disconnect mitigation strategies
│   ├── platform_linux.py   # ip/dnsmasq/nft plumbing + udev + systemd
│   ├── platform_windows.py # SetupAPI / pnputil / HNetCfg / Get-NetAdapter wrappers
│   └── watchdog.py         # Background tethering watchdog
├── itether_windows/        # Windows tray app + INF + PowerShell module
│   ├── iTether.psm1        # Cmdlets: Install-iTetherDriver, Repair-iTetherConnection,
│   │                       #          Enable-iTetherInternetSharing, Get-iTetherStatus, Start-iTetherWatchdog
│   ├── install.ps1         # Installer: stages files, installs INF, creates autostart task
│   ├── INF/
│   │   └── itether_usbncm.inf  # Binds Microsoft UsbNcm.sys to Apple tethering PIDs
│   └── iTether-Tray/       # .NET 10 WinForms tray app (Windows UI)
├── itether_linux/          # Linux tray app + systemd unit + udev rules
│   ├── itether-tray.py     # GTK3 AppIndicator tray
│   ├── systemd/iTether.service
│   ├── udev/99-itether.rules
│   └── install.sh          # Stages files, installs udev rules + systemd unit
└── tests/                  # unittest test suite (descriptor parser + monitor + keepalive)
```

## Build / install

### Windows (10 / 11, x64)

Pre-req: Python 3.10+ and .NET 10 SDK on PATH (Python is used for diagnostics).

```powershell
# 1. Build the tray app (one-time)
cd itether\itether_windows\iTether-Tray
dotnet publish -c Release -r win-x64 --self-contained true

# 2. Run the installer (one-time, admin PowerShell)
cd ..\..
powershell -ExecutionPolicy Bypass -File .\itether_windows\install.ps1
```

The installer:

* Stages `iTether.psm1` and `INF\itether_usbncm.inf` to `%ProgramData%\iTether\`.
* Copies the published `iTetherTray.exe` next to them.
* Runs `pnputil /add-driver itether_usbncm.inf /install`.
* Creates a `Scheduled Task` named `iTether.AutoStart` that launches the
  tray app at every user logon with elevation.
* Starts the tray app now.

Plug in your iPhone, enable **Personal Hotspot**, tap **Trust** on the
phone, and the tray icon turns blue. Right-click → **Show status window**
to see the link state and throughput.

If the iPhone doesn't appear:

* **Tray menu → Repair connection** runs the same operations as
  `Repair-iTetherConnection`.
* **Tray menu → Open log file** shows what happened.

To share the link to other devices:

* **Tray menu → Share connection to other devices…** → pick an Ethernet
  or Wi-Fi adapter that other devices are connected to. iTether configures
  Windows ICS so the iPhone's hotspot reaches the LAN.

To uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File .\itether_windows\install.ps1 -Uninstall
pnputil /delete-driver itether_usbncm.inf
```

### Linux (any modern distro)

The shared `itether_core` library and command-line diagnostics run on Windows,
Linux, and macOS. The desktop shell is platform-native: the Windows build uses
the .NET 10 WinForms tray UI, while Linux uses the GTK/AppIndicator tray.
macOS currently uses the cross-platform CLI/library only.

Pre-req: kernel ≥ 5.x with `cdc_ncm` and `ipheth` available; Python 3.10+;
GTK 3 + AppIndicator (`libappindicator3-1`, `gir1.2-appindicator3-0.1`).

```bash
sudo ./itether/itether_linux/install.sh
```

The installer:

* Copies `itether_core/` + `itether_linux/` to `/usr/local/lib/iTether/`.
* Drops `/etc/udev/rules.d/99-itether.rules` so NetworkManager,
  ModemManager, and usbmuxd don't fight us for the devnode.
* Drops `/etc/systemd/system/iTether.service` and enables it.
* Creates `/usr/local/bin/iTether-tray` and `/usr/local/bin/iTether-diag`.

Launch the tray:

```bash
iTether-tray
```

Or as a user systemd unit (the installer prints a hint when it detects a
graphical session).

Check status from the CLI:

```bash
iTether-diag
# or
python3 -m itether_core diag
```

## What the tool does *not* do

To avoid scope creep and legal issues, iTether does **not**:

* Install Apple Mobile Device Service / the legacy usbmuxd stack.
* Pair with the iPhone for backups, photo sync, or Find My. (Use
  libimobiledevice on Linux for that — iTether co-exists with it.)
* Re-distribute any Apple binaries. `UsbNcm.sys` ships in-box in
  Windows 10 1903+ / Windows 11.
* Vendor any iOS kernel module into Linux. We use the in-tree
  `cdc_ncm` / `ipheth` drivers.

## Compatibility

Confirmed working with iPhone 5 through iPhone 15 (USB-C models require
an Apple Silicon host or recent enough iOS — Apple's NCM switch landed
on iPhone 11 and later with iOS 14, and is the default since iOS 16).

| iOS | Tethering protocol | Linux driver | Windows driver |
|---|---|---|---|
| iOS 3-14 | ipheth bulk pair | `ipheth` | `netaapl64.sys` (we substitute `UsbNcm.sys`) |
| iOS 14-15 | ipheth | `ipheth` | `UsbNcm.sys` |
| iOS 16+ (Lightning) | CDC-NCM, dual function | `cdc_ncm` (interrupt-bearing one) | `UsbNcm.sys` |
| iOS 17+ (USB-C) | CDC-NCM, private | `cdc_ncm` w/ Apple private-interface quirk | `UsbNcm.sys` |

## Testing

```bash
cd itether
make test     # run the unit tests (18 tests)
make demo     # run the end-to-end demo (no iPhone required)
make lint     # syntax-check all Python + PowerShell files
```

Or, equivalently:

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 -m scripts.itether_demo
```

18 tests + a live end-to-end demo cover:

* `lsusb -vvv` parsing for ipheth (iOS 3-15), dual NCM (iOS 16+ with
  RemoteXPC rejection), Apple-private NCM (iOS 17+), and Apple Silicon
  Mac (PID 0x1905).
* Decision-tree selection of the correct tethering function — including
  iOS 18's edge case where the CDC Union descriptor is omitted.
* Class-code tiebreaker: standard CDC-NCM wins over Apple-private NCM
  even at a higher interface number.
* Interrupt-bearing priority: when one NCM has an interrupt-IN endpoint
  and the other does not, the interrupt-bearing one wins regardless of
  interface number.
* No-Union fallback: when the Union descriptor is missing, the parser
  falls back to the +1 heuristic so the data interface is still
  correctly identified.
* Keepalive flag composition.
* Link-sample throughput deltas.
* End-to-end pipeline with mocked hardware: probe → identify → bind →
  monitor → keepalive, with no iPhone required.

## Continuous integration

`.github/workflows/ci.yml` runs `make test`, `make demo`, plus a
PowerShell parser check and a shellcheck pass on every push and PR.
Windows-specific INF syntax is validated by a Python sanity check that
ensures every `%ref%` in the INF has a matching `[Strings]` definition.

## Troubleshooting

See [`docs/troubleshooting.md`](docs/troubleshooting.md) for the full
decision tree covering "USB device not recognized", "90 s disconnect",
random Linux drops, and ICS / masquerade configuration issues.

## License

MIT.
