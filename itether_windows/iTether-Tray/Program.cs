// iTether tray app for Windows
// ============================
//
// Replaces iTunes/Apple Devices for the USB Personal Hotspot use case.
// Lives in the system tray, monitors the Apple tethering adapter, runs
// a watchdog, and exposes:
//   * "Install driver now"     -> Install-iTetherDriver (our INF binding UsbNcm.sys)
//   * "Repair connection"      -> Repair-iTetherConnection (the "USB device has an
//                                 error and can't be recognized" fix)
//   * "Share to other devices" -> Enable-iTetherInternetSharing
//   * Live status tooltip with throughput + state

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Linq;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace iTether;

internal static class Program
{
    [STAThread]
    static void Main()
    {
        Application.SetHighDpiMode(HighDpiMode.SystemAware);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new TrayAppContext());
    }
}

internal sealed class TrayAppContext : ApplicationContext
{
    private readonly NotifyIcon _tray;
    private readonly System.Windows.Forms.Timer _pollTimer;
    private readonly System.Windows.Forms.Timer _statsTimer;
    private readonly string _logPath;
    private bool _sharing;
    private bool _usbOnly = true;
    private readonly Dictionary<int, ProcessPriorityClass> _boostedProcesses = new();
    private string? _adapterName;
    private long _lastRxBytes;
    private long _lastTxBytes;
    private DateTime _lastSample = DateTime.UtcNow;
    private double _rxBps;
    private double _txBps;

    public TrayAppContext()
    {
        var icon = BuildTrayIcon();
        _tray = new NotifyIcon
        {
            Text = "iTether — waiting for iPhone",
            Icon = icon,
            Visible = true,
        };

        var menu = new ContextMenuStrip();
        var installItem = new ToolStripMenuItem("Install driver now")
        {
            ToolTipText = "Stage the iTether INF and bind Microsoft UsbNcm.sys to Apple tethering PIDs.",
        };
        installItem.Click += async (_, _) => await RunPowershellAsync("Install-iTetherDriver");
        menu.Items.Add(installItem);

        var repairItem = new ToolStripMenuItem("Repair connection")
        {
            ToolTipText = "Run the Repair-iTetherConnection sequence: rebind driver, restart adapter, renew DHCP.",
        };
        repairItem.Click += async (_, _) => await RunPowershellAsync("Repair-iTetherConnection");
        menu.Items.Add(repairItem);

        menu.Items.Add(new ToolStripSeparator());

        var settingsItem = new ToolStripMenuItem("Connection settings…");
        settingsItem.Click += (_, _) => new ConnectionSettingsWindow(this).Show();
        menu.Items.Add(settingsItem);

        var boostItem = new ToolStripMenuItem("Power Boost — focus a process…");
        boostItem.Click += (_, _) => new PowerBoostWindow(this).Show();
        menu.Items.Add(boostItem);

        menu.Items.Add(new ToolStripSeparator());

        var shareItem = new ToolStripMenuItem("Share connection to other devices…")
        {
            Enabled = false,
            ToolTipText = "Disabled in USB-only mode. Enable sharing only from the connection settings.",
        };
        shareItem.Click += (_, _) =>
        {
            var pick = PromptPrivateAdapter();
            if (pick is null) return;
            _ = RunPowershellAsync("Enable-iTetherInternetSharing", new() { { "PrivateAdapter", pick } });
            _sharing = true;
        };
        menu.Items.Add(shareItem);

        var unshareItem = new ToolStripMenuItem("Stop sharing");
        unshareItem.Click += async (_, _) =>
        {
            await RunPowershellAsync("Disable-iTetherInternetSharing");
            _sharing = false;
        };
        menu.Items.Add(unshareItem);

        menu.Items.Add(new ToolStripSeparator());

        var statusItem = new ToolStripMenuItem("Show status window…");
        statusItem.Click += (_, _) => new StatusWindow(this).Show();
        menu.Items.Add(statusItem);

        var logItem = new ToolStripMenuItem("Open log file");
        logItem.Click += (_, _) =>
        {
            try
            {
                Process.Start(new ProcessStartInfo
                {
                    FileName = _logPath,
                    UseShellExecute = true,
                });
            }
            catch { /* not running yet, ignore */ }
        };
        menu.Items.Add(logItem);

        menu.Items.Add(new ToolStripSeparator());

        var exitItem = new ToolStripMenuItem("Exit");
        exitItem.Click += (_, _) =>
        {
            _tray.Dispose();
            Application.Exit();
        };
        menu.Items.Add(exitItem);

        _tray.ContextMenuStrip = menu;
        _tray.MouseUp += (_, e) =>
        {
            if (e.Button == MouseButtons.Left)
                new StatusWindow(this).Show();
        };

        _logPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            "iTether", "logs", "tray.log");

        _pollTimer = new System.Windows.Forms.Timer { Interval = 5000 };
        _pollTimer.Tick += async (_, _) => await PollAsync();
        _pollTimer.Start();

        _statsTimer = new System.Windows.Forms.Timer { Interval = 1000 };
        _statsTimer.Tick += (_, _) => UpdateStats();
        _statsTimer.Start();

        Task.Run(EnsureModuleLoadedAsync);
    }

    public string? AdapterName => _adapterName;
    public bool Sharing => _sharing;
    public bool UsbOnly => _usbOnly;
    public double RxBps => _rxBps;
    public double TxBps => _txBps;

    public async Task SetUsbOnlyAsync(bool enabled)
    {
        _usbOnly = enabled;
        if (enabled)
        {
            _sharing = false;
            await RunPowershellAsync(
                "Get-NetAdapter -Name 'Wi-Fi' -ErrorAction SilentlyContinue | Disable-NetAdapter -Confirm:$false");
            await RunPowershellAsync("Disable-iTetherInternetSharing");
        }
    }

    public Task RunRepairAsync() => RunPowershellAsync("Repair-iTetherConnection");

    public void SetProcessBoost(Process process, bool enabled)
    {
        if (enabled)
        {
            if (!_boostedProcesses.ContainsKey(process.Id))
                _boostedProcesses[process.Id] = process.PriorityClass;
            process.PriorityClass = ProcessPriorityClass.AboveNormal;
            process.PriorityBoostEnabled = true;
        }
        else if (_boostedProcesses.Remove(process.Id, out var original))
        {
            try
            {
                process.PriorityClass = original;
                process.PriorityBoostEnabled = false;
            }
            catch (Exception ex) { Log("Could not restore process priority: " + ex.Message); }
        }
    }

    public string? CurrentConnectionSummary =>
        _adapterName is null ? "Connect iPhone by USB and enable Personal Hotspot." :
        $"{_adapterName} • {(_usbOnly ? "USB only" : "sharing enabled")}";

    private async Task PollAsync()
    {
        try
        {
            var adapter = FindAppleAdapter();
            _adapterName = adapter?.Name;
            var tip = adapter is null
                ? "iTether — no iPhone detected"
                : $"iTether — {adapter.Name} ({adapter.Status})\n" +
                  $"RX: {FormatRate(_rxBps)}  TX: {FormatRate(_txBps)}" +
                  (_sharing ? "  • SHARING" : "");
            _tray.Text = tip.Length > 63 ? tip[..63] : tip;
        }
        catch (Exception ex)
        {
            Log("Poll error: " + ex.Message);
        }
        await Task.CompletedTask;
    }

    private void UpdateStats()
    {
        if (_adapterName is null) return;
        try
        {
            var s = GetAdapterStats(_adapterName);
            var now = DateTime.UtcNow;
            var dt = (now - _lastSample).TotalSeconds;
            if (dt > 0.5 && _lastRxBytes != 0)
            {
                _rxBps = (s.RxBytes - _lastRxBytes) * 8.0 / dt;
                _txBps = (s.TxBytes - _lastTxBytes) * 8.0 / dt;
                if (_rxBps < 0) _rxBps = 0;
                if (_txBps < 0) _txBps = 0;
            }
            _lastRxBytes = s.RxBytes;
            _lastTxBytes = s.TxBytes;
            _lastSample = now;
        }
        catch (Exception ex)
        {
            Log("Stats error: " + ex.Message);
        }
    }

    private static string FormatRate(double bps)
    {
        if (bps < 1) return "0 b/s";
        string[] units = { "b/s", "Kb/s", "Mb/s", "Gb/s" };
        int u = 0;
        while (bps >= 1024 && u < units.Length - 1) { bps /= 1024; u++; }
        return $"{bps:0.#} {units[u]}";
    }

    // ------------------------------------------------------------------
    // Adapter discovery + counters (pure WMI, no PowerShell roundtrip)
    // ------------------------------------------------------------------

    private sealed record AdapterRow(string Name, string Status);

    private static AdapterRow? FindAppleAdapter()
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "powershell",
                Arguments = "-NoProfile -Command \"Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'Apple Mobile Device Ethernet|iPhone|USB.*CDC' } | Select-Object Name, Status | ConvertTo-Json -Compress\"",
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var p = Process.Start(psi)!;
            var json = p.StandardOutput.ReadToEnd().Trim();
            p.WaitForExit(2000);
            if (string.IsNullOrWhiteSpace(json)) return null;
            if (!json.StartsWith("{")) return null;
            using var doc = JsonDocument.Parse(json);
            return new AdapterRow(
                doc.RootElement.GetProperty("Name").GetString() ?? "",
                doc.RootElement.GetProperty("Status").GetString() ?? "");
        }
        catch
        {
            return null;
        }
    }

    private sealed record AdapterStats(long RxBytes, long TxBytes);

    private static AdapterStats GetAdapterStats(string name)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "powershell",
                Arguments = $"-NoProfile -Command \"Get-NetAdapterStatistics -Name '{name}' | Select-Object ReceivedBytes, SentBytes | ConvertTo-Json -Compress\"",
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var p = Process.Start(psi)!;
            var json = p.StandardOutput.ReadToEnd().Trim();
            p.WaitForExit(2000);
            if (string.IsNullOrWhiteSpace(json) || !json.StartsWith("{")) return new AdapterStats(0, 0);
            using var doc = JsonDocument.Parse(json);
            long rx = doc.RootElement.TryGetProperty("ReceivedBytes", out var r) ? r.GetInt64() : 0;
            long tx = doc.RootElement.TryGetProperty("SentBytes", out var t) ? t.GetInt64() : 0;
            return new AdapterStats(rx, tx);
        }
        catch
        {
            return new AdapterStats(0, 0);
        }
    }

    // ------------------------------------------------------------------
    // PowerShell plumbing
    // ------------------------------------------------------------------

    private async Task EnsureModuleLoadedAsync()
    {
        await RunPowershellAsync("", importOnly: true);
    }

    private async Task RunPowershellAsync(string cmdlet, Dictionary<string, string>? args = null, bool importOnly = false)
    {
        var sb = new StringBuilder();
        sb.Append("$ErrorActionPreference = 'Continue'; ");
        var modulePath = Path.Combine(AppContext.BaseDirectory, "iTether.psm1");
        sb.Append($"Import-Module '{modulePath}' -Force; ");
        if (importOnly) { sb.Append("Write-Output 'iTether module loaded.'"); }
        else
        {
            sb.Append(cmdlet);
            if (args is { Count: > 0 })
            {
                sb.Append(" ");
                foreach (var kv in args)
                {
                    sb.Append($"-{kv.Key} '{kv.Value}' ");
                }
            }
        }

        var psi = new ProcessStartInfo
        {
            FileName = "powershell",
            Arguments = $"-NoProfile -Command \"{sb}\"",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = false,
            Verb = "runas", // self-elevates
        };
        try
        {
            using var p = Process.Start(psi)!;
            var stdout = await p.StandardOutput.ReadToEndAsync();
            var stderr = await p.StandardError.ReadToEndAsync();
            await p.WaitForExitAsync();
            Log($"PS: {cmdlet} -> rc={p.ExitCode}");
            if (!string.IsNullOrEmpty(stdout)) Log("stdout: " + stdout);
            if (!string.IsNullOrEmpty(stderr)) Log("stderr: " + stderr);
        }
        catch (Exception ex)
        {
            Log("PowerShell failed: " + ex.Message);
        }
    }

    private string? PromptPrivateAdapter()
    {
        // Simple list of network adapters that aren't the iPhone link.
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "powershell",
                Arguments = "-NoProfile -Command \"Get-NetAdapter | Where-Object { $_.InterfaceDescription -notmatch 'Apple Mobile Device Ethernet|iPhone|USB.*CDC' -and $_.Status -eq 'Up' } | Select-Object -ExpandProperty Name | ConvertTo-Json -Compress\"",
                RedirectStandardOutput = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var p = Process.Start(psi)!;
            var json = p.StandardOutput.ReadToEnd().Trim();
            p.WaitForExit(2000);
            if (string.IsNullOrEmpty(json)) return null;
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.ValueKind == JsonValueKind.Array)
            {
                var names = doc.RootElement.EnumerateArray()
                    .Select(e => e.GetString())
                    .Where(s => !string.IsNullOrEmpty(s))
                    .ToArray();
                if (names.Length == 0) return null;
                return names[0]; // for simplicity, take the first
            }
            return doc.RootElement.GetString();
        }
        catch
        {
            return null;
        }
    }

    // ------------------------------------------------------------------
    // Logging
    // ------------------------------------------------------------------

    private void Log(string line)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_logPath)!);
            File.AppendAllText(_logPath, $"[{DateTime.Now:O}] {line}{Environment.NewLine}");
        }
        catch { }
    }

    // ------------------------------------------------------------------
    // Icon (a tiny drawn one — no resource file needed)
    // ------------------------------------------------------------------

    private static Icon BuildTrayIcon()
    {
        using var bmp = new Bitmap(16, 16);
        using var g = Graphics.FromImage(bmp);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        using var bg = new SolidBrush(Color.FromArgb(0, 122, 255));
        g.FillEllipse(bg, 0, 0, 16, 16);
        using var fg = new SolidBrush(Color.White);
        g.FillEllipse(fg, 6, 6, 4, 4);
        g.DrawLine(new Pen(Color.White, 2), 4, 12, 12, 4);
        return Icon.FromHandle(bmp.GetHicon());
    }
}

internal sealed class StatusWindow : Form
{
    private readonly TrayAppContext _ctx;
    private readonly Label _state;
    private readonly Label _throughput;
    private readonly System.Windows.Forms.Timer _t;

    public StatusWindow(TrayAppContext ctx)
    {
        _ctx = ctx;
        Text = "iTether — status";
        Width = 420; Height = 200;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;

        _state = new Label
        {
            Dock = DockStyle.Top,
            Height = 60,
            Font = new Font(Font.FontFamily, 11f, FontStyle.Bold),
            TextAlign = ContentAlignment.MiddleLeft,
            Padding = new Padding(12, 8, 12, 0),
            Text = _ctx.AdapterName is null
                ? "iPhone not detected."
                : $"Adapter: {_ctx.AdapterName}",
        };
        _throughput = new Label
        {
            Dock = DockStyle.Top,
            Height = 60,
            Padding = new Padding(12),
            Text = "RX 0 b/s   TX 0 b/s",
        };
        Controls.Add(_throughput);
        Controls.Add(_state);

        _t = new System.Windows.Forms.Timer { Interval = 1000 };
        _t.Tick += (_, _) =>
        {
            _state.Text = _ctx.AdapterName is null
                ? "iPhone not detected."
                : $"Adapter: {_ctx.AdapterName}   Sharing: {(_ctx.Sharing ? "ON" : "off")}";
            _throughput.Text = $"RX {_ctx.RxBps / 1024.0:0.0} KiB/s   TX {_ctx.TxBps / 1024.0:0.0} KiB/s";
        };
        _t.Start();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        _t.Stop();
        base.OnFormClosing(e);
    }
}

internal sealed class ConnectionSettingsWindow : Form
    {
        private readonly TrayAppContext _ctx;
        private readonly CheckBox _usbOnly;
        private readonly Label _status;

        public ConnectionSettingsWindow(TrayAppContext ctx)
        {
            _ctx = ctx;
            Text = "iTether — connection settings";
            Width = 520;
            Height = 300;
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;

            var heading = new Label
            {
                Text = "Connection profile",
                Font = new Font(Font.FontFamily, 13f, FontStyle.Bold),
                Dock = DockStyle.Top,
                Height = 42,
                Padding = new Padding(16, 12, 0, 0),
            };
            _usbOnly = new CheckBox
            {
                Text = "USB-only mode (recommended)",
                Checked = ctx.UsbOnly,
                AutoSize = true,
                Location = new Point(18, 58),
            };
            var explanation = new Label
            {
                Text = "Uses the iPhone USB Ethernet adapter only. Wi-Fi hotspot sharing and ICS stay off.",
                AutoSize = false,
                Width = 460,
                Height = 42,
                Location = new Point(40, 84),
            };
            var apply = new Button
            {
                Text = "Apply profile",
                AutoSize = true,
                Location = new Point(18, 145),
            };
            apply.Click += async (_, _) =>
            {
                apply.Enabled = false;
                try
                {
                    await _ctx.SetUsbOnlyAsync(_usbOnly.Checked);
                    _status!.Text = "Profile applied.";
                }
                catch (Exception ex) { _status!.Text = "Could not apply: " + ex.Message; }
                finally { apply.Enabled = true; }
            };
            var repair = new Button
            {
                Text = "Repair USB connection",
                AutoSize = true,
                Location = new Point(130, 145),
            };
            repair.Click += async (_, _) =>
            {
                repair.Enabled = false;
                await _ctx.RunRepairAsync();
                _status!.Text = "Repair command sent. Check the status window.";
                repair.Enabled = true;
            };
            _status = new Label
            {
                Text = ctx.CurrentConnectionSummary ?? "No connection detected.",
                AutoSize = false,
                Width = 460,
                Height = 50,
                Location = new Point(18, 195),
                ForeColor = Color.FromArgb(40, 90, 40),
            };
            Controls.AddRange(new Control[] { _status, repair, apply, explanation, _usbOnly, heading });
        }
    }

    internal sealed class PowerBoostWindow : Form
    {
        private readonly TrayAppContext _ctx;
        private readonly ComboBox _processes;
        private readonly CheckBox _enabled;
        private readonly Label _status;
        private readonly List<Process> _processObjects = new();

        public PowerBoostWindow(TrayAppContext ctx)
        {
            _ctx = ctx;
            Text = "iTether — Power Boost";
            Width = 520;
            Height = 285;
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;

            var heading = new Label
            {
                Text = "Focus one workload",
                Font = new Font(Font.FontFamily, 13f, FontStyle.Bold),
                Dock = DockStyle.Top,
                Height = 42,
                Padding = new Padding(16, 12, 0, 0),
            };
            var info = new Label
            {
                Text = "Power Boost gives one selected process Above Normal priority. It is reversible and does not overclock or disable security.",
                Location = new Point(18, 52),
                Width = 465,
                Height = 42,
            };
            _processes = new ComboBox
            {
                DropDownStyle = ComboBoxStyle.DropDownList,
                Location = new Point(18, 105),
                Width = 330,
            };
            _enabled = new CheckBox
            {
                Text = "Boost selected process",
                AutoSize = true,
                Location = new Point(18, 145),
            };
            var refresh = new Button { Text = "Refresh", AutoSize = true, Location = new Point(355, 103) };
            refresh.Click += (_, _) => LoadProcesses();
            _enabled.CheckedChanged += (_, _) => ApplyBoost();
            _status = new Label { AutoSize = false, Width = 465, Height = 50, Location = new Point(18, 185) };
            Controls.AddRange(new Control[] { _status, _enabled, refresh, _processes, info, heading });
            LoadProcesses();
        }

        private void LoadProcesses()
        {
            _processes.Items.Clear();
            _processObjects.Clear();
            foreach (var process in Process.GetProcesses()
                .Where(p => !string.IsNullOrWhiteSpace(p.ProcessName))
                .OrderBy(p => p.ProcessName)
                .GroupBy(p => p.ProcessName, StringComparer.OrdinalIgnoreCase)
                .Select(g => g.First()))
            {
                _processObjects.Add(process);
                _processes.Items.Add($"{process.ProcessName} (PID {process.Id})");
            }
            if (_processes.Items.Count > 0) _processes.SelectedIndex = 0;
            _status.Text = $"{_processes.Items.Count} running processes available.";
        }

        private void ApplyBoost()
        {
            if (_processes.SelectedIndex < 0) return;
            var process = _processObjects[_processes.SelectedIndex];
            try
            {
                _ctx.SetProcessBoost(process, _enabled.Checked);
                _status.Text = _enabled.Checked
                    ? $"Boost active for {process.ProcessName}. Turn it off when the download/model task is done."
                    : $"Boost disabled for {process.ProcessName}.";
            }
            catch (Exception ex)
            {
                _enabled.Checked = false;
                _status.Text = $"Could not change priority: {ex.Message}";
            }
        }
    }
