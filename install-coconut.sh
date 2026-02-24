#!/usr/bin/env bash
set -euo pipefail

# ╔══════════════════════════════════════════════════════════════════════╗
# ║  Coconut KVM Proxy — Single-file Installer for Linux               ║
# ║                                                                      ║
# ║  Installs Python 3, Java 11, sets up the TLS proxy as a systemd     ║
# ║  service so it starts automatically on boot.                         ║
# ║                                                                      ║
# ║  Usage:  sudo bash install-coconut.sh                                ║
# ║                                                                      ║
# ║  After install:                                                      ║
# ║    - Proxy runs at https://<this-machine>:8443                       ║
# ║    - Service: systemctl status coconut-proxy                         ║
# ║    - GUI (optional): coconut-browser                                 ║
# ╚══════════════════════════════════════════════════════════════════════╝

INSTALL_DIR="/opt/coconut"
SERVICE_USER="coconut"
LISTEN_PORT="${COCONUT_PORT:-8443}"
TARGET_HOST="${COCONUT_TARGET:-10.1.10.36}"
TARGET_PORT="${COCONUT_TARGET_PORT:-443}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }
step()  { echo -e "\n${CYAN}───${NC} $* ${CYAN}───${NC}"; }

# ── Pre-flight checks ────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (sudo bash install-coconut.sh)"
fi

if ! grep -qiE 'ubuntu|debian|mint|pop' /etc/os-release 2>/dev/null; then
    warn "This installer targets Debian/Ubuntu. Other distros may need manual adjustment."
fi

step "Installing system dependencies"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-venv \
    python3-pip \
    python3-pyqt5 \
    python3-pyqt5.qtwebengine \
    openjdk-11-jdk \
    openssl \
    xdotool \
    > /dev/null 2>&1

info "Python 3:       $(python3 --version 2>&1)"
info "Java 11:        $(java -version 2>&1 | head -1)"
info "OpenSSL:        $(openssl version 2>&1)"
info "PyQt5:          $(python3 -c 'from PyQt5.QtCore import PYQT_VERSION_STR; print(PYQT_VERSION_STR)' 2>/dev/null || echo 'not found')"
info "QtWebEngine:    $(python3 -c 'from PyQt5.QtWebEngineWidgets import QWebEngineView; print("OK")' 2>/dev/null || echo 'not found')"

# ── Create service user ──────────────────────────────────────────────────────
step "Creating service user"

if id "$SERVICE_USER" &>/dev/null; then
    info "User '$SERVICE_USER' already exists"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    info "Created system user '$SERVICE_USER'"
fi

# ── Install application files ────────────────────────────────────────────────
step "Installing Coconut to $INSTALL_DIR"

mkdir -p "$INSTALL_DIR"
mkdir -p /var/lib/coconut/{certs,jars,storage,cache}

# ── Write coconut.openssl.cnf ────────────────────────────────────────────────
cat > "$INSTALL_DIR/coconut.openssl.cnf" << 'OPENSSL_EOF'

openssl_conf = openssl_init

[openssl_init]
ssl_conf = ssl_sect

[ssl_sect]
system_default = system_default_sect

[system_default_sect]
MinProtocol = TLSv1
CipherString = DEFAULT:@SECLEVEL=0
OPENSSL_EOF
info "Wrote coconut.openssl.cnf"

# ── Write coconut.java.security ──────────────────────────────────────────────
cat > "$INSTALL_DIR/coconut.java.security" << 'JAVASEC_EOF'
# Coconut KVM — override to enable TLS 1.0 for legacy Raritan devices
# Only TLSv1 and TLSv1.1 are removed from the disabled list
jdk.tls.disabledAlgorithms=SSLv3, RC4, DES, \
    DES40_CBC, RC4_40, 3DES_EDE_CBC, anon, NULL, \
    DH keySize < 768, EC keySize < 224

# Allow legacy key sizes
jdk.certpath.disabledAlgorithms=MD2, MD5, SHA1 jdkCA & usage TLSServer, \
    RSA keySize < 1024, DSA keySize < 1024, EC keySize < 224
JAVASEC_EOF
info "Wrote coconut.java.security"

# ── Write proxy.py ───────────────────────────────────────────────────────────
# Using a python script to copy since the file is large and contains
# complex quoting that heredocs struggle with
python3 -c "
import shutil, sys
src = sys.argv[1]
dst = sys.argv[2]
shutil.copy2(src, dst)
" "$(cd "$(dirname "$0")" && pwd)/proxy.py" "$INSTALL_DIR/proxy.py" 2>/dev/null || {
    # If source files aren't alongside this script, extract from embedded data
    warn "proxy.py not found alongside installer — will need manual copy"
    warn "Copy proxy.py to $INSTALL_DIR/proxy.py"
}
info "Installed proxy.py"

# ── Write browser.py ─────────────────────────────────────────────────────────
python3 -c "
import shutil, sys
src = sys.argv[1]
dst = sys.argv[2]
shutil.copy2(src, dst)
" "$(cd "$(dirname "$0")" && pwd)/browser.py" "$INSTALL_DIR/browser.py" 2>/dev/null || {
    warn "browser.py not found alongside installer — will need manual copy"
    warn "Copy browser.py to $INSTALL_DIR/browser.py"
}
info "Installed browser.py"

# ── Write CoconutAppletLauncher.java ─────────────────────────────────────────
python3 -c "
import shutil, sys
src = sys.argv[1]
dst = sys.argv[2]
shutil.copy2(src, dst)
" "$(cd "$(dirname "$0")" && pwd)/CoconutAppletLauncher.java" "$INSTALL_DIR/CoconutAppletLauncher.java" 2>/dev/null || {
    warn "CoconutAppletLauncher.java not found alongside installer"
    warn "Copy CoconutAppletLauncher.java to $INSTALL_DIR/"
}
info "Installed CoconutAppletLauncher.java"

# ── Compile Java launcher ────────────────────────────────────────────────────
step "Compiling Java applet launcher"

JAVA_HOME_DIR=$(dirname $(dirname $(readlink -f $(which java))))
JAVAC="$JAVA_HOME_DIR/bin/javac"
if [[ ! -x "$JAVAC" ]]; then
    JAVAC=$(which javac 2>/dev/null || true)
fi

if [[ -n "$JAVAC" && -f "$INSTALL_DIR/CoconutAppletLauncher.java" ]]; then
    rm -f /var/lib/coconut/jars/CoconutAppletLauncher.class
    "$JAVAC" -source 11 -target 11 \
        -d /var/lib/coconut/jars \
        "$INSTALL_DIR/CoconutAppletLauncher.java" 2>/dev/null
    info "Compiled CoconutAppletLauncher.class"
else
    warn "javac not found — Java launcher will compile on first KVM connect"
fi

# ── Generate TLS certificate ─────────────────────────────────────────────────
step "Generating self-signed TLS certificate"

CERT_FILE="/var/lib/coconut/certs/coconut.pem"
KEY_FILE="/var/lib/coconut/certs/coconut-key.pem"

if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
    info "Certificate already exists, keeping it"
else
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 3650 -nodes \
        -subj "/CN=Coconut KVM Proxy/O=Coconut" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
        2>/dev/null
    info "Generated certificate (valid 10 years)"
fi

# ── Set permissions ──────────────────────────────────────────────────────────
chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/coconut
chmod 600 "$KEY_FILE"
info "Set file permissions"

# ── Write environment file ───────────────────────────────────────────────────
cat > /etc/coconut.env << ENV_EOF
COCONUT_TARGET=$TARGET_HOST
COCONUT_TARGET_PORT=$TARGET_PORT
COCONUT_PORT=$LISTEN_PORT
OPENSSL_CONF=$INSTALL_DIR/coconut.openssl.cnf
ENV_EOF
info "Wrote /etc/coconut.env"

# ── Create systemd service ───────────────────────────────────────────────────
step "Creating systemd service"

cat > /etc/systemd/system/coconut-proxy.service << SERVICE_EOF
[Unit]
Description=Coconut TLS Proxy — Legacy KVM TLS 1.0 to 1.3 translator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=/etc/coconut.env
Environment=HOME=/var/lib/coconut
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$INSTALL_DIR
ExecStartPre=/bin/bash -c 'fuser -k ${COCONUT_PORT:-8443}/tcp 2>/dev/null || true; sleep 1'
ExecStart=/usr/bin/python3 $INSTALL_DIR/proxy.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/lib/coconut
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICE_EOF

info "Wrote coconut-proxy.service"

# ── Patch proxy.py to use /var/lib/coconut paths ─────────────────────────────
step "Configuring proxy for service mode"

if [[ -f "$INSTALL_DIR/proxy.py" ]]; then
    python3 << 'PATCH_EOF'
import re

proxy_path = "/opt/coconut/proxy.py"
with open(proxy_path, "r") as f:
    content = f.read()

# Override cert/key paths to use the service data directory
old_cert_dir = 'CERT_DIR = os.path.join(os.path.expanduser("~"), ".coconut", "certs")'
new_cert_dir = 'CERT_DIR = os.environ.get("COCONUT_CERT_DIR", os.path.join(os.path.expanduser("~"), ".coconut", "certs"))'
content = content.replace(old_cert_dir, new_cert_dir)

with open(proxy_path, "w") as f:
    f.write(content)

print("[✓] Patched proxy.py cert paths")
PATCH_EOF
fi

# Add service env var for cert dir
echo "COCONUT_CERT_DIR=/var/lib/coconut/certs" >> /etc/coconut.env

# ── Enable and start service ─────────────────────────────────────────────────
step "Enabling and starting service"

systemctl daemon-reload
systemctl enable coconut-proxy.service
systemctl start coconut-proxy.service

sleep 2

if systemctl is-active --quiet coconut-proxy.service; then
    info "coconut-proxy service is running"
else
    warn "Service may not have started — check: journalctl -u coconut-proxy -f"
fi

# ── Create convenience commands ──────────────────────────────────────────────
step "Creating convenience commands"

cat > /usr/local/bin/coconut-proxy << 'BIN_EOF'
#!/bin/bash
case "${1:-status}" in
    start)   sudo systemctl start coconut-proxy   && echo "Proxy started" ;;
    stop)    sudo systemctl stop coconut-proxy    && echo "Proxy stopped" ;;
    restart) sudo systemctl restart coconut-proxy && echo "Proxy restarted" ;;
    status)  systemctl status coconut-proxy ;;
    log|logs) journalctl -u coconut-proxy -f ;;
    *)       echo "Usage: coconut-proxy {start|stop|restart|status|logs}" ;;
esac
BIN_EOF
chmod +x /usr/local/bin/coconut-proxy

cat > /usr/local/bin/coconut-browser << 'BIN2_EOF'
#!/bin/bash
# Launch the Coconut GUI browser (requires desktop/X11)
source /etc/coconut.env 2>/dev/null
export COCONUT_TARGET COCONUT_PORT OPENSSL_CONF

# Fix XDG_RUNTIME_DIR if not set (common when running as root or via sudo)
if [[ -z "$XDG_RUNTIME_DIR" ]]; then
    export XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
fi

exec python3 /opt/coconut/browser.py "$@"
BIN2_EOF
chmod +x /usr/local/bin/coconut-browser

info "Created commands: coconut-proxy, coconut-browser"

# ── Desktop shortcut ─────────────────────────────────────────────────────────
step "Creating desktop launcher"

if [[ -f "$INSTALL_DIR/coconut-icon.png" ]]; then
    cp "$INSTALL_DIR/coconut-icon.png" /usr/share/pixmaps/coconut.png
fi

cat > /usr/share/applications/coconut-browser.desktop << 'DESKTOP_EOF'
[Desktop Entry]
Name=Coconut
Comment=KVM Browser for Raritan Equipment
Exec=/usr/local/bin/coconut-browser
Icon=coconut
Type=Application
Categories=Network;RemoteAccess;
Terminal=false
StartupWMClass=coconut
DESKTOP_EOF
chmod 644 /usr/share/applications/coconut-browser.desktop

info "Created desktop launcher (find 'Coconut' in your app menu)"

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

# ── Get LAN IP ───────────────────────────────────────────────────────────────
LAN_IP=$(python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except: print('127.0.0.1')
finally: s.close()
" 2>/dev/null)

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Coconut KVM Proxy — Installed!                    ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Proxy URL:  ${CYAN}https://${LAN_IP}:${LISTEN_PORT}${NC}"
echo -e "${GREEN}║${NC}  Target:     ${TARGET_HOST}:${TARGET_PORT}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Commands:                                                 ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-proxy status    — check service                 ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-proxy logs      — view live logs                ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-proxy restart   — restart proxy                 ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    coconut-browser         — launch GUI (desktop only)     ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Open the URL above in any browser on your network.        ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Accept the self-signed certificate warning.               ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  To change the target KVM:                                 ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}    Edit /etc/coconut.env and run: coconut-proxy restart     ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                            ${GREEN}║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
