#!/usr/bin/env python3
"""
Coconut TLS Proxy — Translates TLS 1.3 (modern browser) to TLS 1.0 (legacy Raritan KVM).

Run this, then open https://localhost:8443 in Chrome/Firefox.
When you click Connect on a KVM port, the standalone Java viewer launches automatically.
"""

import http.server
import http.client
import ssl
import os
import sys
import json
import subprocess
import shutil
import threading
import urllib.request
import tempfile
import re
import socket

# ── Force OpenSSL to allow TLS 1.0 (critical for Linux with OpenSSL 3.x) ────
_openssl_cnf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coconut.openssl.cnf")
if os.path.exists(_openssl_cnf):
    os.environ.setdefault("OPENSSL_CONF", _openssl_cnf)

# ── Configuration ─────────────────────────────────────────────────────────────
TARGET_HOST = os.environ.get("COCONUT_TARGET", "10.1.10.36")
TARGET_PORT = int(os.environ.get("COCONUT_TARGET_PORT", "443"))
LISTEN_PORT = int(os.environ.get("COCONUT_PORT", "8443"))
CERT_DIR = os.environ.get("COCONUT_CERT_DIR", os.path.join(os.path.expanduser("~"), ".coconut", "certs"))
CERT_FILE = os.path.join(CERT_DIR, "coconut.pem")
KEY_FILE = os.path.join(CERT_DIR, "coconut-key.pem")

JAVA_EMULATION_JS = r"""
(function() {
    if (window.__coconutPatched) return;
    window.__coconutPatched = true;

    // ── navigator.javaEnabled ────────────────────────────────────────
    navigator.javaEnabled = function() { return true; };
    try {
        Object.defineProperty(navigator, 'javaEnabled', {
            value: function() { return true; }, writable: false, configurable: true
        });
    } catch(e) {}

    // ── navigator.plugins / mimeTypes ────────────────────────────────
    try {
        var fakePlugins = {
            length: 1, 0: { name: 'Java(TM) Plug-in 11.201.2', filename: 'libnpjp2.so',
                description: 'Next Generation Java Plug-in 11.201.2 for Mozilla browsers',
                length: 1, 0: { type: 'application/x-java-applet', suffixes: 'class,jar' }},
            'Java(TM) Plug-in 11.201.2': { name: 'Java(TM) Plug-in 11.201.2', filename: 'libnpjp2.so',
                description: 'Next Generation Java Plug-in 11.201.2', length: 1 },
            item: function(i) { return this[i]; },
            namedItem: function(n) { return this[n]; },
            refresh: function() {}
        };
        Object.defineProperty(navigator, 'plugins', { get: function() { return fakePlugins; } });
        var fakeMime = {
            length: 1, 0: { type: 'application/x-java-applet', suffixes: 'class,jar',
                description: 'Java Applet', enabledPlugin: fakePlugins[0] },
            'application/x-java-applet': { type: 'application/x-java-applet',
                suffixes: 'class,jar', description: 'Java Applet', enabledPlugin: fakePlugins[0] },
            item: function(i) { return this[i]; },
            namedItem: function(n) { return this[n]; }
        };
        Object.defineProperty(navigator, 'mimeTypes', { get: function() { return fakeMime; } });
    } catch(e) {}

    // ── deployJava shim ──────────────────────────────────────────────
    var _detectionStubs = {
        getJREs: function() { return ['1.8.0_201']; },
        installLatestJRE: function() { return true; },
        isPluginInstalled: function() { return true; },
        isWebStartInstalled: function() { return true; },
        getJPI: function() { return '1.8.0_201'; },
        runApplet: function() { return true; },
        launch: function() { return true; },
        isAutoInstallEnabled: function() { return false; },
        setAutoInstallEnabled: function() {},
        setInstallerType: function() {},
        setAdditionalPackages: function() {},
        setEarlyAccess: function() {},
        returnPage: '',
        locale: 'en',
        browserName: 'Coconut',
        browserName2: 'Coconut',
        do_initialize: function() {}
    };
    var _coconutDJ = Object.assign({}, _detectionStubs);
    try {
        var _origDJ = window.deployJava;
        Object.defineProperty(window, 'deployJava', {
            get: function() { return _coconutDJ; },
            set: function(val) {
                if (val && typeof val === 'object') {
                    for (var k in val) { if (!(k in _detectionStubs)) _coconutDJ[k] = val[k]; }
                    for (var dk in _detectionStubs) _coconutDJ[dk] = _detectionStubs[dk];
                }
            },
            configurable: true, enumerable: true
        });
    } catch(e) { window.deployJava = _coconutDJ; }

    // ── ActiveXObject shim ───────────────────────────────────────────
    window.ActiveXObject = function(name) {
        var lower = (name || '').toLowerCase();
        if (lower.indexOf('xmlhttp') !== -1 || lower.indexOf('msxml2') !== -1 ||
            lower === 'microsoft.xmlhttp') return new XMLHttpRequest();
        if (lower.indexOf('java') !== -1)
            return { object: true, isJavaPluginInstalled: function() { return true; },
                     jvms: { getCount: function() { return 1; } } };
        return {};
    };

    // ── IE DOM shims ─────────────────────────────────────────────────
    if (!document.all) {
        try {
            Object.defineProperty(document, 'all', {
                get: function() {
                    var all = document.querySelectorAll('*');
                    all.item = function(i) { return all[i]; };
                    all.tags = function(t) { return document.getElementsByTagName(t); };
                    all.namedItem = function(n) { return document.getElementById(n) || document.getElementsByName(n)[0]; };
                    return all;
                }, configurable: true
            });
        } catch(e) {}
    }
    if (!Element.prototype.attachEvent) {
        Element.prototype.attachEvent = function(evt, fn) { this.addEventListener(evt.replace(/^on/, ''), fn, false); };
        Element.prototype.detachEvent = function(evt, fn) { this.removeEventListener(evt.replace(/^on/, ''), fn, false); };
    }
    if (!window.attachEvent) {
        window.attachEvent = function(evt, fn) { window.addEventListener(evt.replace(/^on/, ''), fn, false); };
    }
    if (!document.attachEvent) {
        document.attachEvent = function(evt, fn) { document.addEventListener(evt.replace(/^on/, ''), fn, false); };
    }

    // ── LiveConnect objects ───────────────────────────────────────────
    if (typeof window.java === 'undefined') {
        window.java = {
            lang: { System: { getProperty: function(k) {
                if (k === 'java.version') return '1.8.0_201';
                if (k === 'java.vendor') return 'Oracle Corporation';
                return '';
            }}, Class: { forName: function() { return {}; } }, Object: function() {} },
            awt: { Toolkit: { getDefaultToolkit: function() { return {}; } } },
            net: {}, io: {}, util: {}
        };
    }
    if (typeof window.Packages === 'undefined') window.Packages = window.java;
    navigator.Java = window.java;

    // ── dtjava shim ──────────────────────────────────────────────────
    window.dtjava = { install: function(){return true;}, launch: function(){return true;},
                      embed: function(a){window.deployJava.runApplet(a,{},null);} };

    // ── Block window.close / Java alerts ─────────────────────────────
    window.close = function() {};
    var _origConfirm = window.confirm;
    window.confirm = function(msg) {
        if (msg && (/java|jre|jdk|plug-?in|sun\.com/i).test(msg)) return false;
        return _origConfirm.apply(window, arguments);
    };
    var _origAlert = window.alert;
    window.alert = function(msg) {
        if (msg && (/java|jre|jdk|plug-?in|sun\.com/i).test(msg)) return;
        return _origAlert.apply(window, arguments);
    };

    // ── Applet method stubs ──────────────────────────────────────────
    function _dbg(id, method, args, ret) {
        console.log('[Coconut][' + id + '] ' + method + '()');
        return ret;
    }
    var _appletMethods = {
        enableLiveConnect: function() { return _dbg(this.id||'?','enableLiveConnect',arguments,true); },
        toString: function() { return '[JavaApplet]'; },
        validateIPAddress: function() { return 'true'; },
        getVersion: function() { return '1.0'; },
        getOpenTargets: function() { return ''; },
        getUsedPorts: function() { return ''; },
        openTarget: function() { return true; },
        closeTarget: function() { return true; },
        isTargetOpen: function() { return false; },
        getTargetStatus: function() { return ''; },
        favListHasThisKey: function() { return false; },
        favListHasThisDescr: function() { return false; },
        getSortMethod: function() { return '0'; },
        setSortMethod: function() {},
        deleteFromFavorites: function() {},
        writeDeviceToFavorites: function() {},
        overwriteDeviceToFavorites: function() {},
        writeAndShowDeviceToFavorites: function() {},
        showEditDeleteFavorites: function() {},
        getRetrievalDevice: function() {},
        getRetrievalDeviceKeys: function() { return ''; },
        getRetrievalDeviceValues: function() { return ''; },
        getBroadcastPort: function() { return '5000'; },
        saveBroadcastPort: function() {},
        discoverDevices: function() {},
        addServerDiscoveredToFavorites: function() {},
        addClientDiscoveredToFavorites: function() {},
        disconnect: function() { return true; },
        getConnectionInfo: function() { return ''; },
        connect: function(type, portold, pindex, portId, pname, ptype, permString) {
            console.log('[Coconut] CONNECT: port=' + pname + ' id=' + portId);
            var params = {};
            try {
                var applet = document.getElementById('rcApplet');
                if (applet) {
                    var paramEls = applet.querySelectorAll('param');
                    for (var i = 0; i < paramEls.length; i++)
                        params[paramEls[i].name] = paramEls[i].value;
                }
            } catch(e) {}
            params._connect_type = type;
            params._connect_pindex = pindex;
            params._connect_portId = portId;
            params._connect_pname = pname;
            params._connect_ptype = ptype;
            params._connect_host = '""" + TARGET_HOST + r"""';
            // Call the proxy's launch endpoint
            fetch('/__coconut_launch__', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(params)
            }).then(function(r){return r.json();}).then(function(d){
                console.log('[Coconut] Launch response: ' + JSON.stringify(d));
            }).catch(function(e){
                console.log('[Coconut] Launch error: ' + e);
            });
            return true;
        }
    };

    function patchAllApplets() {
        var applets = document.querySelectorAll('applet, object[type*="java"]');
        for (var i = 0; i < applets.length; i++) {
            var a = applets[i];
            if (a.__coconutPatched) continue;
            for (var m in _appletMethods) {
                if (typeof a[m] !== 'function') a[m] = _appletMethods[m];
            }
            a.__coconutPatched = true;
            if (a.id) console.log('[Coconut] Patched applet "' + a.id + '"');
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', patchAllApplets);
    } else {
        patchAllApplets();
    }
    var _patchObs = new MutationObserver(function() { patchAllApplets(); });
    _patchObs.observe(document.documentElement || document.body || document,
                      {childList: true, subtree: true});
})();
"""

CSS_INJECTION = """
<style>applet > * { display: none !important; }</style>
"""


# ── Certificate generation ────────────────────────────────────────────────────

def ensure_certs():
    """Generate a self-signed certificate if one doesn't exist."""
    os.makedirs(CERT_DIR, exist_ok=True)
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return

    print("[Coconut Proxy] Generating self-signed certificate…")

    # Try with -addext first (OpenSSL 1.1.1+), fall back for older versions
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", KEY_FILE, "-out", CERT_FILE,
        "-days", "3650", "-nodes",
        "-subj", "/CN=Coconut KVM Proxy/O=Coconut",
    ]
    r = subprocess.run(cmd + ["-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       capture_output=True)
    if r.returncode != 0:
        subprocess.run(cmd, check=True, capture_output=True)

    print(f"[Coconut Proxy] Certificate saved to {CERT_DIR}")


# ── Backend TLS 1.0 connection ────────────────────────────────────────────────

def _make_legacy_ssl_context():
    """Create an SSL context that can negotiate TLS 1.0 with legacy devices."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Clear OP_NO_TLSv1 flag that OpenSSL 3.x sets by default
    ctx.options &= ~ssl.OP_NO_TLSv1
    ctx.options &= ~ssl.OP_NO_TLSv1_1

    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (ValueError, ssl.SSLError):
        pass  # Some builds refuse even setting the minimum
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2

    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        try:
            ctx.set_ciphers("ALL:@SECLEVEL=0")
        except ssl.SSLError:
            ctx.set_ciphers("ALL")
    return ctx


def make_backend_connection():
    """Create an HTTPS connection to the Raritan KVM using TLS 1.0."""
    ctx = _make_legacy_ssl_context()
    return http.client.HTTPSConnection(TARGET_HOST, TARGET_PORT, context=ctx, timeout=30)


# ── Java launcher ─────────────────────────────────────────────────────────────

def find_java():
    candidates = [
        "/opt/homebrew/opt/openjdk@11/bin/java",
        "/usr/lib/jvm/java-11-openjdk-amd64/bin/java",
        "/usr/lib/jvm/java-11-openjdk/bin/java",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("java")


def launch_kvm_viewer(params):
    """Launch the standalone Java KVM viewer."""
    java = find_java()
    if not java:
        return {"error": "Java 11 not found"}

    host = params.get("_connect_host", TARGET_HOST)
    jar_dir = os.path.join(os.path.expanduser("~"), ".coconut", "jars")
    os.makedirs(jar_dir, exist_ok=True)

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_make_legacy_ssl_context()))

    jar_names = ["rc.jar", "rclang_en.jar"]
    jar_paths = []
    for jar_name in jar_names:
        jp = os.path.join(jar_dir, jar_name)
        if not os.path.exists(jp):
            url = f"https://{host}/{jar_name}"
            try:
                resp = opener.open(url, timeout=10)
                with open(jp, "wb") as f:
                    f.write(resp.read())
                print(f"[Coconut Proxy] Downloaded {jar_name}")
            except Exception as e:
                print(f"[Coconut Proxy] Failed to download {jar_name}: {e}")
                continue
        jar_paths.append(jp)

    launcher_src = os.path.join(os.path.dirname(__file__), "CoconutAppletLauncher.java")
    launcher_class = os.path.join(jar_dir, "CoconutAppletLauncher.class")
    if not os.path.exists(launcher_class) or \
       os.path.getmtime(launcher_src) > os.path.getmtime(launcher_class):
        javac = java.replace("/bin/java", "/bin/javac")
        if not os.path.isfile(javac):
            javac = shutil.which("javac")
        if javac:
            r = subprocess.run(
                [javac, "-source", "11", "-target", "11", "-d", jar_dir, launcher_src],
                capture_output=True, text=True)
            if r.returncode != 0:
                return {"error": f"javac failed: {r.stderr}"}

    sep = ";" if sys.platform == "win32" else ":"
    classpath = sep.join([jar_dir] + jar_paths)
    codebase_url = f"https://{host}/"

    security_file = os.path.join(os.path.dirname(__file__), "coconut.java.security")

    port_name = params.get("_connect_pname", "")
    port_index = params.get("_connect_pindex", "0")
    port_id = params.get("_connect_portId", "")
    session_id = params.get("SESSION_ID", "")
    ssl_port = params.get("SSLPORT", "443")

    applet_params = {
        "java_arguments": "-Xmx512m",
        "BOARD_NAME": params.get("BOARD_NAME", "Dominion KX2"),
        "BOARD_TYPE": params.get("BOARD_TYPE", "lara"),
        "PRODUCT_TYPE": params.get("PRODUCT_TYPE", "kx28"),
        "HW_ID": params.get("HW_ID", "C2"),
        "SESSION_ID": session_id,
        "PORT": params.get("PORT", "443"),
        "SSLPORT": ssl_port,
        "SSL": params.get("SSL", "force"),
        "FIPS": params.get("FIPS", "0"),
        "PORT_ID": port_id,
        "LANGUAGE": params.get("LANGUAGE", "en"),
        "InFrame": "no",
        "CONNECT_PORT_NAME": port_name,
        "CONNECT_INDEX": str(port_index),
        "CONNECT_PORT_ID": port_id,
        "CONNECT_PORT_TYPE": params.get("_connect_ptype", "Dual-VM"),
    }

    cmd = [
        java,
        "-Xms256m", "-Xmx512m",
        "-Djava.awt.headless=false",
        "-Djava.net.preferIPv4Stack=true",
        f"-Djava.security.properties={security_file}",
        "-Djdk.tls.client.protocols=TLSv1,TLSv1.1,TLSv1.2",
        "-Dhttps.protocols=TLSv1,TLSv1.1,TLSv1.2",
        "-Dcom.sun.net.ssl.checkRevocation=false",
        "-cp", classpath,
        "CoconutAppletLauncher",
        "nn.pp.rc.RemoteConsoleApplet",
        codebase_url,
    ]
    for k, v in applet_params.items():
        cmd.append(f"{k}={v}")

    print(f"[Coconut Proxy] Launching KVM viewer for {port_name}…")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=None, stderr=None, cwd=jar_dir)
    return {"status": "launched", "port": port_name}


# ── Proxy request handler ────────────────────────────────────────────────────

BLOCKED_HOSTS = {"java.sun.com", "java.com", "oracle.com", "www.java.com",
                 "www.oracle.com", "download.oracle.com", "javadl.oracle.com"}

SPOOFED_UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) "
              "like Gecko Java/1.8.0_201")


class CoconutProxyHandler(http.server.BaseHTTPRequestHandler):
    """Reverse proxy that translates TLS 1.3 ↔ TLS 1.0 and injects Java emulation."""

    def log_message(self, fmt, *args):
        print(f"[Proxy] {fmt % args}", flush=True)

    def _proxy_origin(self):
        """Return the origin URL that the client used to reach this proxy."""
        host_hdr = self.headers.get("Host", f"localhost:{LISTEN_PORT}")
        return f"https://{host_hdr}"

    def _proxy_request(self, method):
        # Handle the launch API endpoint
        if self.path == "/__coconut_launch__":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else b""
            try:
                params = json.loads(body) if body else {}
                result = launch_kvm_viewer(params)
                self._send_json(200, result)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._send_json(500, {"error": str(e)})
            return

        # Block Java download domains
        if any(d in self.path for d in BLOCKED_HOSTS):
            self._send_json(403, {"blocked": True})
            return

        # Block exit pages
        path_lower = self.path.lower()
        if path_lower.endswith("exit.html") or path_lower.endswith("exit.asp"):
            self._send_json(403, {"blocked": "exit page"})
            return

        # Read request body
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else None

        # Forward to backend
        try:
            conn = make_backend_connection()
            headers = dict(self.headers)
            headers.pop("Host", None)
            headers["Host"] = f"{TARGET_HOST}:{TARGET_PORT}"
            headers["User-Agent"] = SPOOFED_UA

            conn.request(method, self.path, body=body, headers=headers)
            resp = conn.getresponse()

            resp_body = resp.read()

            # Check content type for HTML injection
            content_type = resp.getheader("Content-Type", "")
            if "text/html" in content_type:
                resp_body = self._inject_into_html(resp_body)

            # Rewrite Location headers
            self.send_response(resp.status)
            for key, val in resp.getheaders():
                lower_key = key.lower()
                if lower_key in ("transfer-encoding", "content-length",
                                 "content-encoding", "connection"):
                    continue
                if lower_key == "location":
                    proxy_origin = self._proxy_origin()
                    val = val.replace(f"https://{TARGET_HOST}:{TARGET_PORT}", proxy_origin)
                    val = val.replace(f"https://{TARGET_HOST}", proxy_origin)
                    val = val.replace(f"http://{TARGET_HOST}", proxy_origin)
                if lower_key == "set-cookie":
                    val = val.replace("; Secure", "")
                self.send_header(key, val)

            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()

        except Exception as e:
            error_msg = f"Proxy error: {e}"
            print(f"[Proxy] {error_msg}", flush=True)
            self._send_json(502, {"error": error_msg})

    def _inject_into_html(self, body):
        """Inject Java emulation JS and CSS into HTML responses."""
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            return body

        # Rewrite references to the target host to go through proxy
        proxy_origin = self._proxy_origin()
        text = text.replace(f"https://{TARGET_HOST}:{TARGET_PORT}", proxy_origin)
        text = text.replace(f"https://{TARGET_HOST}", proxy_origin)
        text = text.replace(f"http://{TARGET_HOST}", proxy_origin)

        # Strip meta refresh redirects to Java download sites
        text = re.sub(
            r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*'
            r'(java\.com|java\.sun\.com|oracle\.com)[^>]*>',
            '', text, flags=re.IGNORECASE)

        # Block location.replace/assign redirects to Java sites in inline scripts
        text = re.sub(
            r'(location\s*\.\s*(replace|assign|href\s*=))\s*\(\s*["\'][^"\']*'
            r'(java\.com|java\.sun\.com|oracle\.com)[^"\']*["\']\s*\)',
            'void(0)', text, flags=re.IGNORECASE)

        # Inject our JS before </head> or at the start of <body>
        injection = f"<script>{JAVA_EMULATION_JS}</script>\n{CSS_INJECTION}\n"
        if "</head>" in text:
            text = text.replace("</head>", injection + "</head>", 1)
        elif "<body" in text:
            idx = text.index("<body")
            end = text.index(">", idx)
            text = text[:end+1] + injection + text[end+1:]
        else:
            text = injection + text

        return text.encode("utf-8")

    def _send_json(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._proxy_request("GET")

    def do_POST(self):
        self._proxy_request("POST")

    def do_PUT(self):
        self._proxy_request("PUT")

    def do_DELETE(self):
        self._proxy_request("DELETE")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(http.server.HTTPServer):
    """Handle each request in a new thread."""
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        super().server_bind()

    def process_request(self, request, client_address):
        t = threading.Thread(target=self.process_request_thread,
                             args=(request, client_address))
        t.daemon = True
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    ensure_certs()

    server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), CoconutProxyHandler)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    print(f"""
╔══════════════════════════════════════════════════════════╗
║              Coconut TLS Proxy Running                   ║
╠══════════════════════════════════════════════════════════╣
║                                                          ║
║  Open in your browser:                                   ║
║                                                          ║
║    https://localhost:{LISTEN_PORT:<5}                            ║
║                                                          ║
║  Target: {TARGET_HOST}:{TARGET_PORT:<38}║
║                                                          ║
║  (Accept the self-signed certificate warning)            ║
║  Press Ctrl+C to stop                                    ║
╚══════════════════════════════════════════════════════════╝
""", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Coconut Proxy] Shutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
