#!/usr/bin/env bash
# iTether Linux installer
# -----------------------
# Stage files into /usr/local, install udev rules + systemd unit, and
# start the watchdog. The tray app is started as a user systemd unit if
# we are running in a graphical session.
#
# Re-run with --uninstall to undo.

set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
NAME=iTether

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Please run as root (sudo $0 $*)" >&2
        exit 1
    fi
}

install_files() {
    echo "[iTether] installing files to $PREFIX ..."
    install -d "$PREFIX/lib/$NAME/itether_core"
    install -d "$PREFIX/lib/$NAME/itether_linux"
    install -d "$PREFIX/bin"

    install -m 644 ../itether_core/*.py "$PREFIX/lib/$NAME/itether_core/"
    install -m 644 ../itether_linux/__init__.py "$PREFIX/lib/$NAME/itether_linux/"
    install -m 755 itether-tray.py "$PREFIX/lib/$NAME/itether_linux/"

    cat > "$PREFIX/bin/iTether-tray" <<EOF
#!/usr/bin/env bash
exec /usr/bin/env python3 "$PREFIX/lib/$NAME/itether_linux/itether-tray.py" "\$@"
EOF
    chmod 755 "$PREFIX/bin/iTether-tray"

    cat > "$PREFIX/bin/iTether-diag" <<EOF
#!/usr/bin/env bash
exec /usr/bin/env python3 -m itether_core.platform_linux "\$@"
EOF
    chmod 755 "$PREFIX/bin/iTether-diag"

    echo "[iTether] installing udev rules..."
    install -m 644 ../udev/99-itether.rules /etc/udev/rules.d/99-itether.rules
    udevadm control --reload-rules

    echo "[iTether] installing systemd unit..."
    install -m 644 systemd/iTether.service /etc/systemd/system/iTether.service
    systemctl daemon-reload
    systemctl enable --now iTether.service

    # User-level tray service: best-effort, only if loginctl reports a
    # logged-in graphical session.
    if command -v loginctl >/dev/null && loginctl show-session "$(loginctl | awk '/seat0/{print $1}' | head -n1)" -p Type 2>/dev/null | grep -q 'Type=x11\|Type=wayland'; then
        echo "[iTether] graphical session detected - you can launch the tray with: iTether-tray"
    fi
}

uninstall() {
    echo "[iTether] removing..."
    systemctl disable --now iTether.service || true
    rm -f /etc/systemd/system/iTether.service
    rm -f /etc/udev/rules.d/99-itether.rules
    rm -rf "$PREFIX/lib/$NAME"
    rm -f "$PREFIX/bin/iTether-tray" "$PREFIX/bin/iTether-diag"
    udevadm control --reload-rules || true
    systemctl daemon-reload || true
    echo "[iTether] removed."
}

case "${1:-install}" in
    install) require_root; install_files ;;
    uninstall) require_root; uninstall ;;
    *) echo "Usage: $0 [install|uninstall]" ;;
esac
