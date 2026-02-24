# TLS1 Browser

A Python-based web browser built on PyQt5 and QtWebEngine (Chromium) with explicit **TLS 1.0** support, full **JavaScript** execution, and a modern tabbed interface.

## Features

| Feature | Details |
|---|---|
| **TLS 1.0 Support** | Chromium flags lower the minimum SSL version to TLS 1.0 and certificate errors from legacy servers are accepted gracefully. |
| **JavaScript (V8)** | Fully enabled — clipboard access, `window.open`, local storage, and plugins are all turned on. |
| **Tabbed Browsing** | Open, close, and switch between tabs (`Ctrl+T` / `Ctrl+W`). |
| **Bookmarks** | Add (`Ctrl+D`), view (`Ctrl+B`), and delete bookmarks. Persisted across sessions. |
| **Smart URL Bar** | Autocomplete from history; plain text is sent to Google Search. |
| **Downloads** | Save-as dialog for any file download. |
| **Zoom** | `Ctrl++` / `Ctrl+-` / `Ctrl+0` |
| **Dark UI** | Modern dark theme out of the box. |

## Requirements

- **Python 3.9+**
- **Linux** (X11 or Wayland with XWayland) — also works on macOS for development
- System packages for Qt (see below)

## Installation

### 1. Install system dependencies (Linux)

**Debian / Ubuntu:**

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv \
    libgl1-mesa-glx libegl1 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libxtst6 libnss3 libnspr4 \
    libasound2 fonts-liberation
```

**Fedora:**

```bash
sudo dnf install -y python3 python3-pip mesa-libGL mesa-libEGL \
    libxkbcommon nss nspr alsa-lib liberation-fonts
```

**Arch Linux:**

```bash
sudo pacman -S python python-pip mesa nss nspr alsa-lib \
    libxcomposite libxdamage libxrandr libxtst ttf-liberation
```

### 2. Create a virtual environment & install Python packages

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
source .venv/bin/activate
python browser.py
```

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+T` | New tab |
| `Ctrl+W` | Close tab |
| `Ctrl+Q` | Quit |
| `Alt+←` | Back |
| `Alt+→` | Forward |
| `F5` | Reload |
| `Alt+Home` | Home page |
| `Ctrl+D` | Bookmark page |
| `Ctrl+B` | Show bookmarks |
| `Ctrl++` | Zoom in |
| `Ctrl+-` | Zoom out |
| `Ctrl+0` | Reset zoom |

## TLS 1.0 Details

The browser enables TLS 1.0 through two mechanisms:

1. **Chromium flag** `--ssl-version-min=tls1` lowers the minimum protocol version accepted by the rendering engine.
2. **Qt SSL configuration** sets the default `QSslConfiguration` protocol to `TlsV1_0`, covering any Qt networking done outside the Chromium sandbox.
3. **Certificate errors** from legacy TLS 1.0 servers are accepted so the page still loads.

> **Security note:** TLS 1.0 has known vulnerabilities (BEAST, POODLE). Only enable it when you need to reach legacy servers that don't support TLS 1.2+.

## License

MIT
