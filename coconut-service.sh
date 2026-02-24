#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════════╗
# ║  Coconut — Set up as a boot service                         ║
# ║                                                              ║
# ║  Usage:  sudo bash coconut-service.sh                        ║
# ║                                                              ║
# ║  Creates two services:                                       ║
# ║    1. coconut-proxy  — headless TLS proxy (systemd)          ║
# ║       Runs on boot, even without a desktop login             ║
# ║    2. coconut-browser — GUI auto-launch on desktop login     ║
# ╚══════════════════════════════════════════════════════════════╝

INSTALL_DIR="${COCONUT_DIR:-/opt/coconut}"
LISTEN_PORT="${COCONUT_PORT:-8443}"
TARGET_HOST="${COCONUT_TARGET:-10.1.10.36}"
TARGET_PORT="${COCONUT_TARGET_PORT:-443}"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $*"; }
step() { echo -e "\n${CYAN}───${NC} $* ${CYAN}───${NC}"; }

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo:  sudo bash coconut-service.sh"
    exit 1
fi

# ── Detect the real user (not root) ──────────────────────────────────────────
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
REAL_HOME=$(eval echo "~$REAL_USER")
info "Setting up for user: $REAL_USER"

# ── Environment file ─────────────────────────────────────────────────────────
step "Writing configuration"

cat > /etc/coconut.env << EOF
COCONUT_TARGET=$TARGET_HOST
COCONUT_TARGET_PORT=$TARGET_PORT
COCONUT_PORT=$LISTEN_PORT
COCONUT_CERT_DIR=/var/lib/coconut/certs
OPENSSL_CONF=$INSTALL_DIR/coconut.openssl.cnf
EOF
info "Wrote /etc/coconut.env"

# ── Create data directories ──────────────────────────────────────────────────
mkdir -p /var/lib/coconut/{certs,jars,storage,cache}

# ── Service user for headless proxy ──────────────────────────────────────────
if ! id coconut &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin coconut
    info "Created system user 'coconut'"
fi
chown -R coconut:coconut /var/lib/coconut

# ── Generate certificate if needed ───────────────────────────────────────────
CERT="/var/lib/coconut/certs/coconut.pem"
KEY="/var/lib/coconut/certs/coconut-key.pem"
if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
    step "Generating TLS certificate"
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$KEY" -out "$CERT" \
        -days 3650 -nodes \
        -subj "/CN=Coconut KVM Proxy/O=Coconut" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
        2>/dev/null
    chown coconut:coconut "$CERT" "$KEY"
    chmod 600 "$KEY"
    info "Certificate generated (valid 10 years)"
fi

# ══════════════════════════════════════════════════════════════════════════════
#  1. HEADLESS PROXY SERVICE (systemd — runs on boot, no desktop needed)
# ══════════════════════════════════════════════════════════════════════════════
step "Creating proxy service (systemd)"

cat > /etc/systemd/system/coconut-proxy.service << EOF
[Unit]
Description=Coconut TLS Proxy — KVM TLS 1.0 to 1.3 translator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=coconut
Group=coconut
EnvironmentFile=/etc/coconut.env
Environment=HOME=/var/lib/coconut
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$INSTALL_DIR
ExecStartPre=/bin/bash -c 'fuser -k \${COCONUT_PORT:-8443}/tcp 2>/dev/null || true; sleep 1'
ExecStart=/usr/bin/python3 $INSTALL_DIR/proxy.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/coconut
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable coconut-proxy.service
systemctl restart coconut-proxy.service
info "coconut-proxy enabled and started"

# ══════════════════════════════════════════════════════════════════════════════
#  2. BROWSER GUI AUTO-START (XDG autostart — launches on desktop login)
# ══════════════════════════════════════════════════════════════════════════════
step "Creating browser autostart (desktop login)"

# System-wide autostart: launches for any user who logs in
mkdir -p /etc/xdg/autostart
cat > /etc/xdg/autostart/coconut-browser.desktop << EOF
[Desktop Entry]
Type=Application
Name=Coconut
Comment=KVM Browser for Raritan Equipment
Exec=/usr/local/bin/coconut-browser
Icon=/usr/share/pixmaps/coconut.png
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
StartupWMClass=Coconut
Categories=Network;RemoteAccess;
EOF
info "Browser will auto-launch on desktop login"

# Also put in the app menu
cp /etc/xdg/autostart/coconut-browser.desktop /usr/share/applications/coconut-browser.desktop 2>/dev/null || true

# Install icon to hicolor theme so taskbar/dock picks it up
INSTALL_DIR="/opt/coconut"
if [[ -f "$INSTALL_DIR/coconut-icon.png" ]]; then
    cp "$INSTALL_DIR/coconut-icon.png" /usr/share/pixmaps/coconut.png
    for size in 256 128 64 48; do
        icon_dir="/usr/share/icons/hicolor/${size}x${size}/apps"
        mkdir -p "$icon_dir"
        if command -v convert &>/dev/null; then
            convert "$INSTALL_DIR/coconut-icon.png" -resize "${size}x${size}" "$icon_dir/coconut.png"
        else
            cp "$INSTALL_DIR/coconut-icon.png" "$icon_dir/coconut.png"
        fi
    done
    gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
fi

# Desktop shortcut
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo $USER)}"
REAL_HOME=$(eval echo "~$REAL_USER")
DESKTOP_DIR="$REAL_HOME/Desktop"
if [[ -d "$DESKTOP_DIR" ]]; then
    cp /usr/share/applications/coconut-browser.desktop "$DESKTOP_DIR/coconut-browser.desktop"
    chown "$REAL_USER:$REAL_USER" "$DESKTOP_DIR/coconut-browser.desktop"
    chmod +x "$DESKTOP_DIR/coconut-browser.desktop"
    sudo -u "$REAL_USER" gio set "$DESKTOP_DIR/coconut-browser.desktop" \
        metadata::trusted true 2>/dev/null || true
    info "Placed Coconut shortcut on desktop"
fi

# ── Convenience commands ─────────────────────────────────────────────────────
step "Creating commands"

cat > /usr/local/bin/coconut-proxy << 'BINEOF'
#!/bin/bash
case "${1:-status}" in
    start)   sudo systemctl start coconut-proxy   && echo "Proxy started" ;;
    stop)    sudo systemctl stop coconut-proxy    && echo "Proxy stopped" ;;
    restart) sudo systemctl restart coconut-proxy && echo "Proxy restarted" ;;
    enable)  sudo systemctl enable coconut-proxy  && echo "Proxy enabled on boot" ;;
    disable) sudo systemctl disable coconut-proxy && echo "Proxy disabled on boot" ;;
    status)  systemctl status coconut-proxy ;;
    log|logs) journalctl -u coconut-proxy -f ;;
    *)       echo "Usage: coconut-proxy {start|stop|restart|enable|disable|status|logs}" ;;
esac
BINEOF
chmod +x /usr/local/bin/coconut-proxy

cat > /usr/local/bin/coconut-browser << 'BINEOF'
#!/bin/bash
source /etc/coconut.env 2>/dev/null
export COCONUT_TARGET COCONUT_PORT OPENSSL_CONF
if [[ -z "$XDG_RUNTIME_DIR" ]]; then
    export XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
fi
exec python3 /opt/coconut/browser.py "$@"
BINEOF
chmod +x /usr/local/bin/coconut-browser

info "Commands: coconut-proxy, coconut-browser"

# ── Open firewall port ───────────────────────────────────────────────────────
step "Configuring firewall"

if command -v ufw &>/dev/null; then
    ufw allow "$LISTEN_PORT"/tcp comment "Coconut TLS Proxy" 2>/dev/null
    info "Opened port $LISTEN_PORT/tcp in ufw"
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port="$LISTEN_PORT"/tcp 2>/dev/null
    firewall-cmd --reload 2>/dev/null
    info "Opened port $LISTEN_PORT/tcp in firewalld"
else
    info "No firewall detected — port $LISTEN_PORT should be open"
fi

# ── Status ───────────────────────────────────────────────────────────────────
step "Status"

LAN_IP=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except: print('127.0.0.1')
finally: s.close()
" 2>/dev/null)

PROXY_STATUS="stopped"
systemctl is-active --quiet coconut-proxy && PROXY_STATUS="running"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              Coconut — Service Setup Complete               ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Proxy:    https://${LAN_IP}:${LISTEN_PORT}  [${PROXY_STATUS}]"
echo -e "${GREEN}║${NC}  Target:   ${TARGET_HOST}:${TARGET_PORT}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  On boot:                                                  ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    ✓ Proxy starts automatically (systemd)                  ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    ✓ Browser opens on desktop login (XDG autostart)        ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Commands:                                                 ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-proxy status     — check proxy                  ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-proxy logs       — live proxy logs              ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-proxy restart    — restart proxy                ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-browser          — launch browser manually      ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Config:   /etc/coconut.env                                ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Disable:  coconut-proxy disable                           ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
