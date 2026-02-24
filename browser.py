#!/usr/bin/env python3
"""
Coconut — Dedicated browser for Raritan and legacy Java-applet equipment.

Fakes a full Java runtime environment so pages never redirect to java.sun.com.
Actually executes applets via CheerpJ (in-browser JVM) or a local Java install.
TLS 1.0 is enabled for legacy HTTPS management interfaces.
"""

import sys
import os
import subprocess
import shutil
import tempfile
import urllib.request
import ssl
import socket
import threading

# ── Chromium flags (set BEFORE QApplication) ────────────────────────────────
# NOTE: --proxy-server is added dynamically in main() once we know the port.
_CHROMIUM_BASE_FLAGS = [
    "--ssl-version-min=tls1",
    "--ignore-certificate-errors",
    "--allow-running-insecure-content",
    "--disable-web-security",
    "--reduce-security-for-testing",
]
sys.argv += _CHROMIUM_BASE_FLAGS

import struct
import time

from PyQt5.QtCore import Qt, QUrl, QSize, QSettings, QStringListModel, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QKeySequence, QFont, QPalette, QColor, QImage, QPainter
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QToolBar, QAction,
    QLineEdit, QStatusBar, QWidget, QVBoxLayout, QHBoxLayout,
    QMenu, QMenuBar, QDialog, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QProgressBar, QShortcut, QSizePolicy, QMessageBox,
    QFileDialog, QStyle, QCompleter,
)
from PyQt5.QtWebEngineWidgets import (
    QWebEngineView, QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
    QWebEngineDownloadItem, QWebEngineScript,
)
from PyQt5.QtNetwork import QSslConfiguration, QSsl

# Domains that Raritan pages redirect to when they think Java is missing.
BLOCKED_DOMAINS = [
    "java.sun.com",
    "java.com",
    "oracle.com",
    "javadl.oracle.com",
    "javadl.sun.com",
    "www.java.com",
    "www.oracle.com",
    "download.oracle.com",
]

# User-Agent: IE 11 on Windows 7 with Java/1.8.0_201 appended — this is
# exactly what the JRE adds to the UA string when installed on Windows.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0; "
    ".NET4.0C; .NET4.0E; .NET CLR 2.0.50727; .NET CLR 3.0.30729; "
    ".NET CLR 3.5.30729; Java/1.8.0_201) like Gecko"
)


# ── Colour palette ──────────────────────────────────────────────────────────
DARK_BG       = "#1e1e2e"
DARK_SURFACE  = "#282840"
DARK_BORDER   = "#3b3b5c"
ACCENT        = "#7c3aed"
ACCENT_HOVER  = "#6d28d9"
TEXT_PRIMARY   = "#e2e2f0"
TEXT_SECONDARY = "#9090b0"
TAB_ACTIVE_BG = "#332e55"

STYLESHEET = f"""
QMainWindow {{
    background-color: {DARK_BG};
}}
QTabWidget::pane {{
    border: none;
    background: {DARK_BG};
}}
QTabBar {{
    background: {DARK_SURFACE};
    border: none;
}}
QTabBar::tab {{
    background: {DARK_SURFACE};
    color: {TEXT_SECONDARY};
    padding: 8px 18px;
    margin: 2px 1px 0 1px;
    border: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    min-width: 120px;
    max-width: 240px;
    font-size: 13px;
}}
QTabBar::tab:selected {{
    background: {TAB_ACTIVE_BG};
    color: {TEXT_PRIMARY};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover:!selected {{
    background: {DARK_BORDER};
    color: {TEXT_PRIMARY};
}}
QToolBar {{
    background: {DARK_SURFACE};
    border: none;
    padding: 4px 6px;
    spacing: 4px;
}}
QToolButton {{
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 5px;
    color: {TEXT_SECONDARY};
    font-size: 16px;
}}
QToolButton:hover {{
    background: {DARK_BORDER};
    color: {TEXT_PRIMARY};
}}
QLineEdit {{
    background: {DARK_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {DARK_BORDER};
    border-radius: 16px;
    padding: 6px 14px;
    font-size: 14px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QStatusBar {{
    background: {DARK_SURFACE};
    color: {TEXT_SECONDARY};
    font-size: 12px;
    border-top: 1px solid {DARK_BORDER};
}}
QProgressBar {{
    background: {DARK_BG};
    border: none;
    border-radius: 2px;
    height: 3px;
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 2px;
}}
QMenu {{
    background: {DARK_SURFACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
    padding: 4px 0;
}}
QMenu::item {{
    padding: 6px 28px;
}}
QMenu::item:selected {{
    background: {ACCENT};
}}
QMenuBar {{
    background: {DARK_SURFACE};
    color: {TEXT_SECONDARY};
    border: none;
}}
QMenuBar::item:selected {{
    background: {DARK_BORDER};
    color: {TEXT_PRIMARY};
}}
QMessageBox {{
    background: {DARK_SURFACE};
    color: {TEXT_PRIMARY};
}}
QLabel {{
    color: {TEXT_PRIMARY};
}}
QListWidget {{
    background: {DARK_BG};
    color: {TEXT_PRIMARY};
    border: 1px solid {DARK_BORDER};
    border-radius: 6px;
}}
QListWidget::item:selected {{
    background: {ACCENT};
}}
QPushButton {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 13px;
}}
QPushButton:hover {{
    background: {ACCENT_HOVER};
}}
"""


# ═══════════════════════════════════════════════════════════════════════════
#  JavaScript injected at DocumentCreation (BEFORE any page scripts run).
#
#  The critical trick is the Object.defineProperty setter/getter trap on
#  window.deployJava.  When the Raritan page loads Oracle's real
#  deployJava.js, the `var deployJava = { ... }` assignment triggers our
#  setter.  We copy the real object's properties into ours, then re-apply
#  our detection stubs — all *synchronously*, before the next inline
#  <script> can call checkJavaSupport().
# ═══════════════════════════════════════════════════════════════════════════
JAVA_PLUGIN_EMULATION_JS = r"""
(function() {
    'use strict';

    // ── Domains we never allow a redirect to ───────────────────────────
    var BLOCKED = ['java.sun.com','java.com','oracle.com','javadl.oracle.com',
                   'javadl.sun.com','download.oracle.com','www.java.com',
                   'www.oracle.com'];

    function isBlocked(url) {
        if (!url || typeof url !== 'string') return false;
        for (var i = 0; i < BLOCKED.length; i++) {
            if (url.indexOf(BLOCKED[i]) !== -1) return true;
        }
        return false;
    }

    // ── Block location.replace / location.assign ───────────────────────
    try {
        var _replace = window.location.replace.bind(window.location);
        window.location.replace = function(u) {
            if (isBlocked(u)) { console.log('[Coconut] Blocked redirect -> ' + u); return; }
            if (u === 'exit.html' || (u && u.indexOf('exit.html') !== -1)) {
                console.log('[Coconut] Blocked redirect -> exit.html');
                return;
            }
            return _replace(u);
        };
    } catch(e) {}
    try {
        var _assign = window.location.assign.bind(window.location);
        window.location.assign = function(u) {
            if (isBlocked(u)) { console.log('[Coconut] Blocked redirect -> ' + u); return; }
            return _assign(u);
        };
    } catch(e) {}

    // ── Block window.open to Java download sites ───────────────────────
    var _open = window.open;
    window.open = function(u) {
        if (isBlocked(u)) { console.log('[Coconut] Blocked popup -> ' + u); return null; }
        return _open.apply(window, arguments);
    };

    // ── Block <meta http-equiv="refresh"> pointing to Java sites ───────
    try {
        var metaObs = new MutationObserver(function(muts) {
            muts.forEach(function(m) {
                m.addedNodes.forEach(function(n) {
                    if (n.tagName === 'META' && n.httpEquiv &&
                        n.httpEquiv.toLowerCase() === 'refresh' &&
                        n.content && isBlocked(n.content)) {
                        n.remove();
                    }
                });
            });
        });
        metaObs.observe(document.documentElement || document, { childList: true, subtree: true });
    } catch(e) {}

    // ── navigator.javaEnabled() -> true ─────────────────────────────────
    try {
        Object.defineProperty(navigator, 'javaEnabled', {
            value: function() { return true; },
            configurable: true, writable: false
        });
    } catch(e) {}

    // ── Fake Java plugin & mime-types ──────────────────────────────────
    var javaPlugin = {
        name: 'Java(TM) Plug-in 11.201.2',
        description: 'Java Applet Plug-in (JRE 1.8.0_201)',
        filename: 'libnpjp2.so',
        length: 3
    };
    var mimeApplet = { type: 'application/x-java-applet',    suffixes: 'class,jar', description: 'Java Applet',    enabledPlugin: javaPlugin };
    var mimeBean   = { type: 'application/x-java-bean',      suffixes: 'class,jar', description: 'Java Bean',      enabledPlugin: javaPlugin };
    var mimeJNLP   = { type: 'application/x-java-jnlp-file', suffixes: 'jnlp',      description: 'Java Web Start', enabledPlugin: javaPlugin };
    javaPlugin[0] = mimeApplet;
    javaPlugin[1] = mimeBean;
    javaPlugin[2] = mimeJNLP;

    try {
        var origPlugins = navigator.plugins;
        var fakePlugins = {
            length: origPlugins.length + 1,
            item:      function(i) { return (i === origPlugins.length) ? javaPlugin : origPlugins.item(i); },
            namedItem: function(n) { return (n === javaPlugin.name) ? javaPlugin : origPlugins.namedItem(n); },
            refresh:   function() {}
        };
        for (var i = 0; i < origPlugins.length; i++) {
            fakePlugins[i] = origPlugins[i];
            if (origPlugins[i] && origPlugins[i].name) fakePlugins[origPlugins[i].name] = origPlugins[i];
        }
        fakePlugins[origPlugins.length] = javaPlugin;
        fakePlugins[javaPlugin.name]    = javaPlugin;
        Object.defineProperty(navigator, 'plugins', { get: function() { return fakePlugins; }, configurable: true });
    } catch(e) {}

    try {
        var origMimes = navigator.mimeTypes;
        var extraMimes = [mimeApplet, mimeBean, mimeJNLP];
        var fakeMimes = {
            length: origMimes.length + extraMimes.length,
            item:      function(i) { return (i < origMimes.length) ? origMimes.item(i) : extraMimes[i - origMimes.length]; },
            namedItem: function(n) {
                for (var j = 0; j < extraMimes.length; j++) { if (extraMimes[j].type === n) return extraMimes[j]; }
                return origMimes.namedItem(n);
            }
        };
        for (var i = 0; i < origMimes.length; i++) fakeMimes[i] = origMimes[i];
        for (var j = 0; j < extraMimes.length; j++) {
            fakeMimes[origMimes.length + j] = extraMimes[j];
            fakeMimes[extraMimes[j].type]   = extraMimes[j];
        }
        Object.defineProperty(navigator, 'mimeTypes', { get: function() { return fakeMimes; }, configurable: true });
    } catch(e) {}

    // ── Detection method stubs (shared between shim & setter trap) ────
    var _detectionStubs = {
        getJREs:              function() { return ['1.8.0_201']; },
        installJRE:           function() { return true; },
        installLatestJRE:     function() { return true; },
        isPluginInstalled:    function() { return true; },
        isWebStartInstalled:  function() { return '1.8.0_201'; },
        versionCheck:         function() { return true; },
        getPlugin:            function() { return javaPlugin; },
        isPlugin2:            function() { return true; },
        allowPlugin:          function() { return true; },
        getBrowser:           function() { return 'MSIE'; },
        getJPIVersionUsingMimeType: function() { return '1.8.0_201'; },
        compareVersionToPattern:    function() { return true; },
        isAutoInstallEnabled: function() { return false; },
        isAutoUpdateEnabled:  function() { return false; }
    };

    // ── deployJava setter/getter trap ──────────────────────────────────
    // Our shim object starts with detection stubs + useful write methods.
    var _coconutDJ = {
        returnPage: null, locale: null, brand: null, installType: null,
        debug: null, firefoxJavaVersion: null, myInterval: null,
        preInstallJREList: null, EAInstallEnabled: false, EarlyAccessURL: null,
        setInstallerType:      function(t) { this.installType = t; },
        setAdditionalPackages: function() {},
        writeAppletTag: function(attrs) {
            var applet = document.createElement('applet');
            if (attrs) for (var k in attrs) if (attrs.hasOwnProperty(k)) applet.setAttribute(k, attrs[k]);
            var place = function() { (document.body || document.documentElement).appendChild(applet); };
            document.body ? place() : document.addEventListener('DOMContentLoaded', place);
        },
        writeEmbedTag:  function(a) { this.writeAppletTag(a); },
        writeObjectTag: function(a) { this.writeAppletTag(a); },
        runApplet: function(attrs, params) {
            var applet = document.createElement('applet');
            if (attrs) for (var k in attrs) if (attrs.hasOwnProperty(k)) applet.setAttribute(k, attrs[k]);
            if (params) for (var k in params) if (params.hasOwnProperty(k)) {
                var p = document.createElement('param'); p.name = k; p.value = params[k]; applet.appendChild(p);
            }
            var place = function() { (document.body || document.documentElement).appendChild(applet); };
            document.body ? place() : document.addEventListener('DOMContentLoaded', place);
        },
        launch: function(url) {
            if (url && url.indexOf('.jnlp') !== -1) window.location.href = url;
            return true;
        }
    };
    for (var dk in _detectionStubs) _coconutDJ[dk] = _detectionStubs[dk];

    // The trap: when Oracle's real deployJava.js does
    //   var deployJava = { ... };
    // the setter fires synchronously.  We merge the real object's
    // properties into ours, then re-apply detection stubs so the
    // next inline <script> sees Java as installed.
    try {
        Object.defineProperty(window, 'deployJava', {
            get: function() { return _coconutDJ; },
            set: function(val) {
                if (val && typeof val === 'object') {
                    for (var k in val) {
                        if (val.hasOwnProperty(k)) _coconutDJ[k] = val[k];
                    }
                    for (var dk in _detectionStubs) _coconutDJ[dk] = _detectionStubs[dk];
                    console.log('[Coconut] deployJava setter trap fired — detection methods patched');
                }
            },
            configurable: true,
            enumerable: true
        });
    } catch(e) {
        window.deployJava = _coconutDJ;
    }

    // ── ActiveX / IE compatibility layer ──────────────────────────────
    window.ActiveXObject = function(name) {
        var lower = (name || '').toLowerCase();
        if (lower.indexOf('xmlhttp') !== -1 || lower.indexOf('msxml2') !== -1 ||
            lower === 'microsoft.xmlhttp') {
            return new XMLHttpRequest();
        }
        if (lower.indexOf('xmldom') !== -1 || lower.indexOf('domdocument') !== -1 ||
            lower.indexOf('msxml') !== -1) {
            var dp = new DOMParser();
            dp.loadXML = function(str) {
                var doc = dp.parseFromString(str, 'text/xml');
                for (var k in doc) { try { dp[k] = doc[k]; } catch(e){} }
                dp.xml = str; dp.documentElement = doc.documentElement;
                return doc;
            };
            dp.xml = '';
            return dp;
        }
        if (lower.indexOf('java') !== -1) {
            return { object: true, isJavaPluginInstalled: function() { return true; },
                     jvms: { getCount: function() { return 1; } } };
        }
        console.log('[Coconut] ActiveXObject stub for: ' + name);
        return {};
    };
    window.ActiveXObject.toString = function() { return 'function ActiveXObject() { [native code] }'; };

    // ── IE-specific DOM/BOM shims ─────────────────────────────────────
    if (!document.all) {
        try {
            Object.defineProperty(document, 'all', {
                get: function() {
                    var all = document.querySelectorAll('*');
                    all.item = function(i) { return all[i]; };
                    all.tags = function(t) { return document.getElementsByTagName(t); };
                    all.namedItem = function(n) { return document.getElementById(n) || document.getElementsByName(n)[0]; };
                    return all;
                },
                configurable: true
            });
        } catch(e) {}
    }
    if (typeof window.event === 'undefined') {
        document.addEventListener('click',     function(e) { window.event = e; }, true);
        document.addEventListener('keydown',   function(e) { window.event = e; }, true);
        document.addEventListener('keyup',     function(e) { window.event = e; }, true);
        document.addEventListener('mousemove', function(e) { window.event = e; }, true);
        document.addEventListener('submit',    function(e) { window.event = e; }, true);
    }
    if (!Element.prototype.attachEvent) {
        Element.prototype.attachEvent = function(evt, fn) { this.addEventListener(evt.replace(/^on/, ''), fn, false); };
        Element.prototype.detachEvent = function(evt, fn) { this.removeEventListener(evt.replace(/^on/, ''), fn, false); };
    }
    if (!window.attachEvent) {
        window.attachEvent = function(evt, fn) { window.addEventListener(evt.replace(/^on/, ''), fn, false); };
        window.detachEvent = function(evt, fn) { window.removeEventListener(evt.replace(/^on/, ''), fn, false); };
    }
    if (!document.attachEvent) {
        document.attachEvent = function(evt, fn) { document.addEventListener(evt.replace(/^on/, ''), fn, false); };
        document.detachEvent = function(evt, fn) { document.removeEventListener(evt.replace(/^on/, ''), fn, false); };
    }

    // ── dtjava.js shim ────────────────────────────────────────────────
    window.dtjava = window.dtjava || {};
    window.dtjava.install = function() { return true; };
    window.dtjava.launch  = function() { return true; };
    window.dtjava.embed   = function(attrs) { window.deployJava.runApplet(attrs, {}, null); };

    // ── Block window.close after failed Java check ────────────────────
    window.close = function() { console.log('[Coconut] Blocked window.close()'); };

    // ── Intercept confirm/alert that mention Java ─────────────────────
    var _origConfirm = window.confirm;
    window.confirm = function(msg) {
        if (msg && (/java|jre|jdk|plug-?in|sun\.com/i).test(msg)) {
            console.log('[Coconut] Auto-cancelled Java confirm');
            return false;
        }
        return _origConfirm.apply(window, arguments);
    };
    var _origAlert = window.alert;
    window.alert = function(msg) {
        if (msg && (/java|jre|jdk|plug-?in|sun\.com/i).test(msg)) {
            console.log('[Coconut] Suppressed Java alert');
            return;
        }
        return _origAlert.apply(window, arguments);
    };

    // ── Global Java / Packages objects (LiveConnect) ──────────────────
    if (typeof window.java === 'undefined') {
        window.java = {
            lang: {
                System: {
                    getProperty: function(k) {
                        if (k === 'java.version') return '1.8.0_201';
                        if (k === 'java.vendor') return 'Oracle Corporation';
                        return '';
                    }
                },
                Class: { forName: function() { return {}; } },
                Object: function() {}
            },
            awt: { Toolkit: { getDefaultToolkit: function() { return {}; } } },
            net: {}, io: {}, util: {}
        };
    }
    if (typeof window.Packages === 'undefined') window.Packages = window.java;
    navigator.Java = window.java;

    // ── Comprehensive applet method stubs ─────────────────────────────
    // Both dpaApplet (dpa.util.Nav) and rcApplet (RemoteConsoleApplet)
    // need method stubs so page JS doesn't crash.
    // Every stub logs its call for debugging.
    function _dbg(appletId, method, args, retVal) {
        var a = Array.prototype.slice.call(args);
        console.log('[Coconut][' + appletId + '] ' + method + '(' + a.join(', ') + ')' + (retVal !== undefined ? ' -> ' + JSON.stringify(retVal) : ''));
        return retVal;
    }

    var _appletMethods = {
        // Common
        enableLiveConnect:              function() { return _dbg(this.id||'?', 'enableLiveConnect', arguments, true); },
        toString:                       function() { return '[JavaApplet ' + (this.id||'') + ']'; },
        // dpaApplet methods
        validateIPAddress:              function() { return _dbg(this.id||'?', 'validateIPAddress', arguments, 'true'); },
        getVersion:                     function() { return _dbg(this.id||'?', 'getVersion', arguments, '1.0'); },
        deleteFromFavorites:            function() { _dbg(this.id||'?', 'deleteFromFavorites', arguments); },
        writeDeviceToFavorites:         function() { _dbg(this.id||'?', 'writeDeviceToFavorites', arguments); },
        overwriteDeviceToFavorites:     function() { _dbg(this.id||'?', 'overwriteDeviceToFavorites', arguments); },
        writeAndShowDeviceToFavorites:  function() { _dbg(this.id||'?', 'writeAndShowDeviceToFavorites', arguments); },
        getOpenTargets:                 function() { return _dbg(this.id||'?', 'getOpenTargets', arguments, ''); },
        openTarget:                     function() { return _dbg(this.id||'?', 'openTarget', arguments, true); },
        closeTarget:                    function() { return _dbg(this.id||'?', 'closeTarget', arguments, true); },
        isTargetOpen:                   function() { return _dbg(this.id||'?', 'isTargetOpen', arguments, false); },
        getTargetStatus:                function() { return _dbg(this.id||'?', 'getTargetStatus', arguments, ''); },
        favListHasThisKey:              function() { return _dbg(this.id||'?', 'favListHasThisKey', arguments, false); },
        favListHasThisDescr:            function() { return _dbg(this.id||'?', 'favListHasThisDescr', arguments, false); },
        getSortMethod:                  function() { return _dbg(this.id||'?', 'getSortMethod', arguments, '0'); },
        setSortMethod:                  function() { _dbg(this.id||'?', 'setSortMethod', arguments); },
        showEditDeleteFavorites:        function() { _dbg(this.id||'?', 'showEditDeleteFavorites', arguments); },
        getRetrievalDevice:             function() { _dbg(this.id||'?', 'getRetrievalDevice', arguments); },
        getRetrievalDeviceKeys:         function() { return _dbg(this.id||'?', 'getRetrievalDeviceKeys', arguments, ''); },
        getRetrievalDeviceValues:       function() { return _dbg(this.id||'?', 'getRetrievalDeviceValues', arguments, ''); },
        getBroadcastPort:               function() { return _dbg(this.id||'?', 'getBroadcastPort', arguments, '5000'); },
        saveBroadcastPort:              function() { _dbg(this.id||'?', 'saveBroadcastPort', arguments); },
        discoverDevices:                function() { _dbg(this.id||'?', 'discoverDevices', arguments); },
        addServerDiscoveredToFavorites: function() { _dbg(this.id||'?', 'addServerDiscoveredToFavorites', arguments); },
        addClientDiscoveredToFavorites: function() { _dbg(this.id||'?', 'addClientDiscoveredToFavorites', arguments); },
        // rcApplet methods (RemoteConsoleApplet)
        getUsedPorts:                   function() { return _dbg(this.id||'?', 'getUsedPorts', arguments, ''); },
        connect: function(type, portold, pindex, portId, pname, ptype, permString) {
            console.log('[Coconut][rcApplet] *** CONNECT REQUESTED ***');
            console.log('[Coconut][rcApplet]   type=' + type + ' portold=' + portold + ' pindex=' + pindex);
            console.log('[Coconut][rcApplet]   portId=' + portId + ' pname=' + pname + ' ptype=' + ptype);
            console.log('[Coconut][rcApplet]   permString=' + permString);

            // Collect rcApplet <param> values for external launch
            var params = {};
            try {
                var applet = document.getElementById('rcApplet');
                if (applet) {
                    var paramEls = applet.querySelectorAll('param');
                    for (var i = 0; i < paramEls.length; i++) {
                        params[paramEls[i].name] = paramEls[i].value;
                    }
                }
            } catch(e) {}
            params._connect_type = type;
            params._connect_pindex = pindex;
            params._connect_portId = portId;
            params._connect_pname = pname;
            params._connect_ptype = ptype;
            params._connect_host = window.location.hostname;

            // Signal to Python side via console
            console.log('[COCONUT_CONNECT] ' + JSON.stringify(params));
            return true;
        },
        disconnect:                     function() { return _dbg(this.id||'?', 'disconnect', arguments, true); },
        getConnectionInfo:              function() { return _dbg(this.id||'?', 'getConnectionInfo', arguments, ''); }
    };

    function patchAllApplets() {
        var applets = document.querySelectorAll('applet, object[type*="java"]');
        for (var i = 0; i < applets.length; i++) {
            var a = applets[i];
            var patched = false;
            for (var k in _appletMethods) {
                if (typeof a[k] !== 'function') {
                    try { a[k] = _appletMethods[k]; patched = true; } catch(e) {}
                }
            }
            if (patched && a.id) {
                console.log('[Coconut] Patched applet "' + a.id + '" with method stubs');
            }
        }
    }

    document.addEventListener('DOMContentLoaded', patchAllApplets);

    try {
        var appletObs = new MutationObserver(function(muts) {
            var found = false;
            muts.forEach(function(m) {
                m.addedNodes.forEach(function(n) {
                    if (n.tagName === 'APPLET' || n.tagName === 'OBJECT' ||
                        (n.querySelector && n.querySelector('applet, object[type*="java"]'))) {
                        found = true;
                    }
                });
            });
            if (found) patchAllApplets();
        });
        if (document.documentElement) {
            appletObs.observe(document.documentElement, { childList: true, subtree: true });
        }
    } catch(e) {}

    // ── Hide fallback content inside <applet> tags ────────────────────
    // Browsers render child elements of <applet> when Java is not
    // available.  Inject CSS to hide them (the "Browser has no Java!" text).
    function injectCSS() {
        var target = document.head || document.documentElement;
        if (!target) return;
        var style = document.createElement('style');
        style.textContent = 'applet > * { display: none !important; }';
        target.appendChild(style);
    }
    try { injectCSS(); } catch(e) {}
    document.addEventListener('DOMContentLoaded', injectCSS);

    console.log('[Coconut] Java 1.8.0_201 emulation active');
})();
"""




# ═══════════════════════════════════════════════════════════════════════════
#  Post-load cleanup: hides "Java not found" fallback content and
#  ensures all applet stubs are in place.  Runs after the page finishes.
# ═══════════════════════════════════════════════════════════════════════════
PAGE_CLEANUP_JS = r"""
(function() {
    // Hide fallback content inside <applet> tags ("Browser has no Java!")
    document.querySelectorAll('applet').forEach(function(a) {
        for (var i = 0; i < a.children.length; i++) {
            a.children[i].style.display = 'none';
        }
    });

    // Re-verify applet stubs are in place (belt-and-suspenders)
    var methods = {
        enableLiveConnect: function() { return true; },
        getUsedPorts:      function() { return ''; },
        connect:           function() { return true; },
        disconnect:        function() { return true; },
        validateIPAddress: function(ip) { return 'true'; },
        getOpenTargets:    function() { return ''; },
        getBroadcastPort:  function() { return '5000'; },
        getSortMethod:     function() { return '0'; },
        setSortMethod:     function() {},
        discoverDevices:   function() {},
        favListHasThisKey: function() { return false; }
    };
    document.querySelectorAll('applet, object[type*="java"]').forEach(function(a) {
        for (var k in methods) {
            if (typeof a[k] !== 'function') a[k] = methods[k];
        }
    });

    console.log('[Coconut] Post-load cleanup complete');
})();
"""


RARITAN_HOSTS = {"10.1.10.36"}


def _configure_global_ssl():
    config = QSslConfiguration.defaultConfiguration()
    config.setProtocol(QSsl.TlsV1_0)
    QSslConfiguration.setDefaultConfiguration(config)


def _install_user_scripts(profile: QWebEngineProfile):
    scripts = profile.scripts()

    s1 = QWebEngineScript()
    s1.setName("TLS1_JavaEmulation")
    s1.setSourceCode(JAVA_PLUGIN_EMULATION_JS)
    s1.setInjectionPoint(QWebEngineScript.DocumentCreation)
    s1.setWorldId(QWebEngineScript.MainWorld)
    s1.setRunsOnSubFrames(True)
    scripts.insert(s1)

    s2 = QWebEngineScript()
    s2.setName("Coconut_PageCleanup")
    s2.setSourceCode(PAGE_CLEANUP_JS)
    s2.setInjectionPoint(QWebEngineScript.DocumentReady)
    s2.setWorldId(QWebEngineScript.MainWorld)
    s2.setRunsOnSubFrames(True)
    scripts.insert(s2)


def _is_blocked_url(url: QUrl) -> bool:
    host = url.host().lower()
    for d in BLOCKED_DOMAINS:
        if host == d or host.endswith("." + d):
            return True
    return False


# ── Custom page: blocks Java-site redirects + handles applets ──────────────
class BrowserPage(QWebEnginePage):
    _JAVA_KEYWORDS = ("java", "jre", "jdk", "plug-in", "plugin", "sun.com")

    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self._java_fallback_offered = False

    def certificateError(self, error):
        error.ignoreCertificateError()
        return True

    # ── Auto-dismiss Java-related JS dialogs ────────────────────────────
    def javaScriptAlert(self, origin, msg):
        if any(kw in msg.lower() for kw in self._JAVA_KEYWORDS):
            print(f"[SUPPRESSED ALERT] {msg}", flush=True)
            return
        super().javaScriptAlert(origin, msg)

    def javaScriptConfirm(self, origin, msg):
        if any(kw in msg.lower() for kw in self._JAVA_KEYWORDS):
            print(f"[SUPPRESSED CONFIRM → Cancel] {msg}", flush=True)
            return False
        return super().javaScriptConfirm(origin, msg)

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if _is_blocked_url(url):
            print(f"[NAV BLOCKED] {url.toString()}", flush=True)
            return False
        path = url.path().lower()
        if path.endswith("exit.html") or path.endswith("exit.asp"):
            print(f"[NAV BLOCKED] exit page: {url.toString()}", flush=True)
            return False
        return True

    def javaScriptConsoleMessage(self, level, message, line, source_id):
        tag = ["INFO", "WARN", "ERROR"][min(level, 2)]
        print(f"[JS {tag}] {source_id}:{line}  {message}", flush=True)

        if "[COCONUT_CONNECT]" in message:
            import json
            try:
                json_str = message.split("[COCONUT_CONNECT] ", 1)[1]
                params = json.loads(json_str)
                main = self.view().window()
                if isinstance(main, BrowserWindow):
                    main._launch_kvm_viewer(params)
            except Exception as e:
                print(f"[Coconut] Failed to parse connect params: {e}", flush=True)

    def createWindow(self, window_type):
        main = self.view().window()
        if isinstance(main, BrowserWindow):
            return main.add_new_tab().page()
        return super().createWindow(window_type)


class BrowserTab(QWebEngineView):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        page = BrowserPage(profile, self)
        self.setPage(page)

        s = self.settings()
        s.setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
        s.setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
        s.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        s.setAttribute(QWebEngineSettings.PluginsEnabled, True)
        s.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, True)
        s.setAttribute(QWebEngineSettings.AllowRunningInsecureContent, True)
        s.setAttribute(QWebEngineSettings.AllowGeolocationOnInsecureOrigins, True)
        s.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)

    def createWindow(self, window_type):
        return self.page().createWindow(window_type)


class BookmarkDialog(QDialog):
    def __init__(self, bookmarks, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bookmarks")
        self.setMinimumSize(480, 360)
        self.bookmarks = bookmarks
        self.selected_url = None

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        for title, url in self.bookmarks:
            item = QListWidgetItem(f"{title}\n{url}")
            item.setData(Qt.UserRole, url)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open)
        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(self._delete)
        btn_row.addWidget(open_btn)
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

    def _open(self):
        item = self.list_widget.currentItem()
        if item:
            self.selected_url = item.data(Qt.UserRole)
            self.accept()

    def _delete(self):
        row = self.list_widget.currentRow()
        if row >= 0:
            self.list_widget.takeItem(row)
            del self.bookmarks[row]


# ── KVM Viewer Widget (embedded frame-streaming viewer) ─────────────────────
_QT_TO_JAVA_KEY = {
    Qt.Key_Return: 10, Qt.Key_Enter: 10, Qt.Key_Escape: 27, Qt.Key_Tab: 9,
    Qt.Key_Backspace: 8, Qt.Key_Delete: 127, Qt.Key_Insert: 155,
    Qt.Key_Home: 36, Qt.Key_End: 35, Qt.Key_PageUp: 33, Qt.Key_PageDown: 34,
    Qt.Key_Up: 38, Qt.Key_Down: 40, Qt.Key_Left: 37, Qt.Key_Right: 39,
    Qt.Key_Shift: 16, Qt.Key_Control: 17, Qt.Key_Alt: 18, Qt.Key_Meta: 157,
    Qt.Key_CapsLock: 20, Qt.Key_NumLock: 144, Qt.Key_ScrollLock: 145,
    Qt.Key_F1: 112, Qt.Key_F2: 113, Qt.Key_F3: 114, Qt.Key_F4: 115,
    Qt.Key_F5: 116, Qt.Key_F6: 117, Qt.Key_F7: 118, Qt.Key_F8: 119,
    Qt.Key_F9: 120, Qt.Key_F10: 121, Qt.Key_F11: 122, Qt.Key_F12: 123,
    Qt.Key_Space: 32,
}


class KvmViewerWidget(QWidget):
    """Captures the Java applet window via macOS Quartz and forwards
    keyboard/mouse events through the subprocess pipes."""

    disconnected = pyqtSignal()

    def __init__(self, proc, parent=None):
        super().__init__(parent)
        self._proc = proc
        self._current_frame = None
        self._scroll_lock_time = 0.0
        self._alive = True
        self._java_wid = None
        self._frame_w = 1024
        self._frame_h = 768

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.BlankCursor)

        self.disconnected.connect(self._on_disconnect)

        self._capture_timer = QTimer(self)
        self._capture_timer.timeout.connect(self._capture_frame)
        QTimer.singleShot(2000, lambda: self._capture_timer.start(33))

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._check_proc)
        self._poll_timer.start(500)

    def _find_java_window(self):
        """Find the Java applet window named 'CoconutKVM' by PID."""
        try:
            from Quartz import (CGWindowListCopyWindowInfo,
                                kCGWindowListOptionAll, kCGNullWindowID)
            windows = CGWindowListCopyWindowInfo(
                kCGWindowListOptionAll, kCGNullWindowID)
            pid = self._proc.pid
            for w in windows:
                if w.get('kCGWindowOwnerPID', 0) != pid:
                    continue
                name = w.get('kCGWindowName', '')
                wid = w.get('kCGWindowNumber', 0)
                bounds = w.get('kCGWindowBounds', {})
                bw = int(bounds.get('Width', 0))
                bh = int(bounds.get('Height', 0))
                if name == 'CoconutKVM' and bw > 100 and bh > 100:
                    self._frame_w = bw
                    self._frame_h = bh
                    print(f"[Coconut] Found Java window: id={wid} "
                          f"size={bw}x{bh}", flush=True)
                    return wid
        except Exception as e:
            print(f"[Coconut] Window search error: {e}", flush=True)
        return None

    def _capture_frame(self):
        """Capture the Java window using CGWindowListCreateImage."""
        if not self._alive:
            return
        if self._proc.poll() is not None:
            self._on_disconnect()
            return

        if self._java_wid is None:
            self._java_wid = self._find_java_window()
            if self._java_wid is None:
                return

        try:
            from Quartz import (CGWindowListCreateImage, CGRectNull,
                                kCGWindowListOptionIncludingWindow,
                                kCGWindowImageDefault,
                                kCGWindowImageBoundsIgnoreFraming,
                                CGImageGetWidth, CGImageGetHeight,
                                CGImageGetBytesPerRow, CGImageGetDataProvider,
                                CGDataProviderCopyData,
                                CGImageGetBitsPerPixel,
                                CGImageGetAlphaInfo)

            cg_image = CGWindowListCreateImage(
                CGRectNull,
                kCGWindowListOptionIncludingWindow,
                self._java_wid,
                kCGWindowImageBoundsIgnoreFraming)

            if cg_image is None:
                if not hasattr(self, '_null_logged'):
                    print("[Coconut] CGWindowListCreateImage returned None",
                          flush=True)
                    self._null_logged = True
                return

            w = CGImageGetWidth(cg_image)
            h = CGImageGetHeight(cg_image)
            bpr = CGImageGetBytesPerRow(cg_image)
            bpp = CGImageGetBitsPerPixel(cg_image)
            alpha = CGImageGetAlphaInfo(cg_image)

            if not hasattr(self, '_capture_count'):
                self._capture_count = 0
            self._capture_count += 1
            if self._capture_count <= 3 or self._capture_count % 60 == 0:
                print(f"[Coconut] Capture #{self._capture_count}: "
                      f"{w}x{h} bpr={bpr} bpp={bpp} alpha={alpha}",
                      flush=True)

            if w < 10 or h < 10:
                return

            provider = CGImageGetDataProvider(cg_image)
            data = CGDataProviderCopyData(provider)

            if self._capture_count <= 3:
                sample = bytes(data[:16]) if data else b''
                print(f"[Coconut] First 16 bytes: {sample.hex()}",
                      flush=True)

            img = QImage(data, w, h, bpr, QImage.Format_ARGB32_Premultiplied)
            if self._capture_count <= 3:
                print(f"[Coconut] QImage null={img.isNull()} "
                      f"size={img.width()}x{img.height()}",
                      flush=True)
            img = img.rgbSwapped()
            self._current_frame = img.copy()
            self._frame_w = w
            self._frame_h = h
            self.update()
        except Exception as e:
            print(f"[Coconut] Capture error: {e}", flush=True)

    def _check_proc(self):
        if self._proc.poll() is not None and self._alive:
            self._on_disconnect()

    def _on_disconnect(self):
        self._alive = False
        self._capture_timer.stop()
        self._poll_timer.stop()
        try:
            parent = self.parent()
            if parent and hasattr(parent, '_on_kvm_disconnect'):
                QTimer.singleShot(0, parent._on_kvm_disconnect)
        except RuntimeError:
            pass

    def paintEvent(self, event):
        if self._current_frame and not self._current_frame.isNull():
            p = QPainter(self)
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.drawImage(self.rect(), self._current_frame)
            p.end()
        else:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor("#1e1e2e"))
            p.setPen(QColor("#e2e2f0"))
            p.setFont(QFont("sans-serif", 20))
            p.drawText(self.rect(), Qt.AlignCenter, "Connecting to KVM…")
            p.end()

    # ── Mouse events ──────────────────────────────────────────────────

    def _scale_pos(self, event):
        if not self._current_frame or self._current_frame.isNull():
            return event.x(), event.y()
        sx = self._current_frame.width() / max(self.width(), 1)
        sy = self._current_frame.height() / max(self.height(), 1)
        return int(event.x() * sx), int(event.y() * sy)

    def _btn_mask(self, event):
        b = 0
        btns = event.buttons()
        if btns & Qt.LeftButton:   b |= 1
        if btns & Qt.RightButton:  b |= 2
        if btns & Qt.MiddleButton: b |= 4
        return b

    def mouseMoveEvent(self, event):
        x, y = self._scale_pos(event)
        if event.buttons():
            self._send_event(b'D', struct.pack('>HHB', x, y, self._btn_mask(event)))
        else:
            self._send_event(b'M', struct.pack('>HHB', x, y, 0))

    def mousePressEvent(self, event):
        self.setFocus()
        x, y = self._scale_pos(event)
        self._send_event(b'P', struct.pack('>HHB', x, y, self._btn_mask(event)))

    def mouseReleaseEvent(self, event):
        x, y = self._scale_pos(event)
        self._send_event(b'L', struct.pack('>HHB', x, y, self._btn_mask(event)))

    # ── Keyboard events ──────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_ScrollLock:
            now = time.time()
            if now - self._scroll_lock_time < 0.6:
                self._disconnect()
                return
            self._scroll_lock_time = now
        self._send_key(event, pressed=True)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        self._send_key(event, pressed=False)

    def _send_key(self, event, pressed):
        java_key = _QT_TO_JAVA_KEY.get(event.key())
        if java_key is None:
            k = event.key()
            if 0x20 <= k <= 0x7E:
                java_key = k
            else:
                return
        mods = 0
        m = event.modifiers()
        if m & Qt.ShiftModifier:   mods |= 1
        if m & Qt.ControlModifier: mods |= 2
        if m & Qt.AltModifier:     mods |= 4
        if m & Qt.MetaModifier:    mods |= 8
        self._send_event(b'K', struct.pack('>iBB', java_key, 1 if pressed else 0, mods))

    # ── Send / disconnect ────────────────────────────────────────────

    def _send_event(self, tag, data):
        if not self._alive or not self._proc or self._proc.poll() is not None:
            return
        try:
            self._proc.stdin.write(tag + data)
            self._proc.stdin.flush()
        except Exception:
            pass

    def _disconnect(self):
        self._alive = False
        try:
            self._proc.stdin.write(b'Q')
            self._proc.stdin.flush()
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass

    def cleanup(self):
        self._alive = False
        try:
            self._proc.terminate()
        except Exception:
            pass


# ── TLS Proxy (integrated) ───────────────────────────────────────────────────

def _get_lan_ip():
    """Get this machine's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class ProxyManager:
    """Manages the TLS proxy lifecycle — start/stop from the GUI."""

    def __init__(self, target_host, target_port=443, listen_port=8443):
        self._target_host = target_host
        self._target_port = target_port
        self._listen_port = listen_port
        self._server = None
        self._thread = None
        self.running = False

    @property
    def url(self):
        ip = _get_lan_ip()
        return f"https://{ip}:{self._listen_port}"

    def start(self):
        if self.running:
            return
        from proxy import (CoconutProxyHandler, ThreadedHTTPServer,
                           ensure_certs, CERT_FILE, KEY_FILE,
                           _make_legacy_ssl_context)
        import proxy as proxy_mod

        proxy_mod.TARGET_HOST = self._target_host
        proxy_mod.TARGET_PORT = self._target_port
        proxy_mod.LISTEN_PORT = self._listen_port

        ensure_certs()

        self._server = ThreadedHTTPServer(
            ("0.0.0.0", self._listen_port), CoconutProxyHandler)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT_FILE, KEY_FILE)
        self._server.socket = ctx.wrap_socket(
            self._server.socket, server_side=True)

        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.running = True
        print(f"[Coconut] TLS Proxy started on {self.url}", flush=True)

    def stop(self):
        if not self.running:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception as e:
            print(f"[Coconut] Proxy stop error: {e}", flush=True)
        self._server = None
        self._thread = None
        self.running = False
        print("[Coconut] TLS Proxy stopped", flush=True)


# ── Main window ─────────────────────────────────────────────────────────────
class BrowserWindow(QMainWindow):
    HOME_URL = f"https://{os.environ.get('COCONUT_TARGET', '10.1.10.36')}"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Coconut — Raritan Console")
        self.resize(1280, 820)

        self.bookmarks: list[tuple[str, str]] = []
        self.history_urls: list[str] = []
        self.settings_store = QSettings("Coconut", "Coconut")
        self._load_bookmarks()

        # Dedicated profile with its own storage — avoids SQLite cookie
        # lock conflicts with any other QtWebEngine instance.
        data_dir = os.path.join(os.path.expanduser("~"), ".coconut")
        os.makedirs(data_dir, exist_ok=True)

        self._profile = QWebEngineProfile("Coconut", self)
        self._profile.setPersistentStoragePath(os.path.join(data_dir, "storage"))
        self._profile.setCachePath(os.path.join(data_dir, "cache"))
        self._profile.setHttpCacheMaximumSize(256 * 1024 * 1024)
        self._profile.setHttpUserAgent(USER_AGENT)
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.ForcePersistentCookies
        )
        self._profile.downloadRequested.connect(self._on_download)

        _install_user_scripts(self._profile)

        target_host = QUrl(self.HOME_URL).host()
        self._proxy = ProxyManager(target_host=target_host)

        self._build_menu_bar()
        self._build_toolbar()
        self._build_tabs()
        self._build_statusbar()

        self.add_new_tab(QUrl(self.HOME_URL))

        QTimer.singleShot(500, self._auto_start_proxy)

    def _build_menu_bar(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("&File")
        file_menu.addAction("New Tab", self.add_new_tab, QKeySequence("Ctrl+T"))
        file_menu.addAction("Close Tab", self._close_current_tab, QKeySequence("Ctrl+W"))
        file_menu.addSeparator()
        file_menu.addAction("Quit", self.close, QKeySequence("Ctrl+Q"))

        nav_menu = mb.addMenu("&Navigate")
        nav_menu.addAction("Back", self._back, QKeySequence("Alt+Left"))
        nav_menu.addAction("Forward", self._forward, QKeySequence("Alt+Right"))
        nav_menu.addAction("Reload", self._reload, QKeySequence("F5"))
        nav_menu.addAction("Home", self._go_home, QKeySequence("Alt+Home"))

        bm_menu = mb.addMenu("&Bookmarks")
        bm_menu.addAction("Bookmark This Page", self._add_bookmark, QKeySequence("Ctrl+D"))
        bm_menu.addAction("Show Bookmarks…", self._show_bookmarks, QKeySequence("Ctrl+B"))

        view_menu = mb.addMenu("&View")
        view_menu.addAction("Zoom In", self._zoom_in, QKeySequence("Ctrl++"))
        view_menu.addAction("Zoom Out", self._zoom_out, QKeySequence("Ctrl+-"))
        view_menu.addAction("Reset Zoom", self._zoom_reset, QKeySequence("Ctrl+0"))

        help_menu = mb.addMenu("&Help")
        help_menu.addAction("About", self._show_about)

    def _build_toolbar(self):
        tb = QToolBar("Navigation")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)

        self.act_back    = tb.addAction("◀", self._back)
        self.act_forward = tb.addAction("▶", self._forward)
        self.act_reload  = tb.addAction("⟳", self._reload)
        self.act_home    = tb.addAction("⌂", self._go_home)

        self.url_bar = QLineEdit()
        self.url_bar.setPlaceholderText("Enter URL or search…")
        self.url_bar.returnPressed.connect(self._navigate_to_url)
        self.url_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self._url_completer_model = QStringListModel()
        completer = QCompleter()
        completer.setModel(self._url_completer_model)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setMaxVisibleItems(8)
        self.url_bar.setCompleter(completer)

        tb.addWidget(self.url_bar)

        self.act_bookmark = tb.addAction("★", self._add_bookmark)
        self.act_bookmark.setToolTip("Bookmark this page")

        tb.addSeparator()

        self.proxy_btn = QPushButton("  Proxy OFF  ")
        self.proxy_btn.setCheckable(True)
        self.proxy_btn.setToolTip("Start/stop TLS proxy for network access")
        self.proxy_btn.clicked.connect(self._toggle_proxy)
        self.proxy_btn.setStyleSheet(f"""
            QPushButton {{
                background: {DARK_BG};
                color: {TEXT_SECONDARY};
                border: 1px solid {DARK_BORDER};
                border-radius: 12px;
                padding: 5px 14px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                border-color: {ACCENT};
            }}
            QPushButton:checked {{
                background: #164e2a;
                color: #4ade80;
                border: 1px solid #22c55e;
            }}
            QPushButton:checked:hover {{
                background: #1a6334;
            }}
        """)
        tb.addWidget(self.proxy_btn)

        self.act_newtab = tb.addAction("+", self.add_new_tab)
        self.act_newtab.setToolTip("New tab")

    def _build_tabs(self):
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

    def _build_statusbar(self):
        self.status = QStatusBar()
        self.setStatusBar(self.status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(160)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        self.status.addPermanentWidget(self.progress_bar)

        self.proxy_label = QLabel("  ○  Proxy OFF")
        self.proxy_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px; padding: 0 12px;")
        self.status.addPermanentWidget(self.proxy_label)

        self.ssl_label = QLabel()
        self.status.addPermanentWidget(self.ssl_label)

    # ── Tab management ──────────────────────────────────────────────────
    def add_new_tab(self, url=None):
        if isinstance(url, bool) or url is None:
            url = QUrl(self.HOME_URL)
        elif isinstance(url, str):
            url = QUrl(url)

        view = BrowserTab(self._profile, self)
        idx = self.tabs.addTab(view, "New Tab")
        self.tabs.setCurrentIndex(idx)

        view.titleChanged.connect(lambda t, v=view: self._update_tab_title(v, t))
        view.urlChanged.connect(lambda u, v=view: self._update_url_bar(v, u))
        view.loadStarted.connect(self._on_load_started)
        view.loadProgress.connect(self._on_load_progress)
        view.loadFinished.connect(self._on_load_finished)
        view.page().linkHovered.connect(self._on_link_hovered)

        view.setUrl(url)
        return view

    def _close_tab(self, idx):
        if self.tabs.count() > 1:
            w = self.tabs.widget(idx)
            self.tabs.removeTab(idx)
            w.deleteLater()
        else:
            self.close()

    def _close_current_tab(self):
        self._close_tab(self.tabs.currentIndex())

    def _current_view(self) -> BrowserTab | None:
        return self.tabs.currentWidget()

    # ── Navigation ──────────────────────────────────────────────────────
    def _back(self):
        v = self._current_view()
        if v: v.back()

    def _forward(self):
        v = self._current_view()
        if v: v.forward()

    def _reload(self):
        v = self._current_view()
        if v: v.reload()

    def _go_home(self):
        v = self._current_view()
        if v: v.setUrl(QUrl(self.HOME_URL))

    def _navigate_to_url(self):
        text = self.url_bar.text().strip()
        if not text:
            return
        if " " in text or "." not in text:
            url = QUrl(f"https://www.google.com/search?q={text}")
        elif not text.startswith(("http://", "https://")):
            url = QUrl(f"http://{text}")
        else:
            url = QUrl(text)
        v = self._current_view()
        if v: v.setUrl(url)

    # ── Signals ─────────────────────────────────────────────────────────
    def _update_tab_title(self, view, title):
        idx = self.tabs.indexOf(view)
        if idx >= 0:
            short = (title[:28] + "…") if len(title) > 30 else title
            self.tabs.setTabText(idx, short)
            if view == self._current_view():
                self.setWindowTitle(f"{title} — Coconut")

    def _update_url_bar(self, view, qurl):
        if view == self._current_view():
            url_str = qurl.toString()
            self.url_bar.setText(url_str)
            self._record_history(url_str)
            self._update_ssl_indicator(qurl)

    def _on_tab_changed(self, idx):
        v = self._current_view()
        if v:
            self.url_bar.setText(v.url().toString())
            title = v.page().title() or "New Tab"
            self.setWindowTitle(f"{title} — Coconut")
            self._update_ssl_indicator(v.url())

    def _on_load_started(self):
        self.progress_bar.setValue(0)
        self.progress_bar.show()

    def _on_load_progress(self, p):
        self.progress_bar.setValue(p)

    def _on_load_finished(self, ok):
        self.progress_bar.hide()
        if not ok:
            self.status.showMessage("Page failed to load", 4000)

    def _on_link_hovered(self, url):
        self.status.showMessage(url, 3000)

    def _update_ssl_indicator(self, qurl):
        if qurl.scheme() == "https":
            self.ssl_label.setText("🔒 TLS")
            self.ssl_label.setStyleSheet("color: #22c55e; font-weight: bold;")
        else:
            self.ssl_label.setText("🔓 HTTP")
            self.ssl_label.setStyleSheet("color: #f59e0b; font-weight: bold;")

    def _record_history(self, url_str):
        if url_str and url_str not in self.history_urls:
            self.history_urls.append(url_str)
            if len(self.history_urls) > 500:
                self.history_urls = self.history_urls[-500:]
            self._url_completer_model.setStringList(self.history_urls)

    # ── Bookmarks ───────────────────────────────────────────────────────
    def _add_bookmark(self):
        v = self._current_view()
        if not v: return
        title = v.page().title()
        url = v.url().toString()
        for _, u in self.bookmarks:
            if u == url:
                self.status.showMessage("Already bookmarked", 2000)
                return
        self.bookmarks.append((title, url))
        self._save_bookmarks()
        self.status.showMessage(f"Bookmarked: {title}", 2000)

    def _show_bookmarks(self):
        dlg = BookmarkDialog(self.bookmarks, self)
        if dlg.exec_() == QDialog.Accepted and dlg.selected_url:
            v = self._current_view()
            if v: v.setUrl(QUrl(dlg.selected_url))
        self._save_bookmarks()

    def _save_bookmarks(self):
        self.settings_store.setValue("bookmarks", list(self.bookmarks))

    def _load_bookmarks(self):
        saved = self.settings_store.value("bookmarks")
        if saved and isinstance(saved, list):
            self.bookmarks = saved

    # ── Downloads / JNLP ───────────────────────────────────────────────
    def _on_download(self, download: QWebEngineDownloadItem):
        default_path = download.path()

        if default_path.endswith(".jnlp"):
            self._handle_jnlp(download)
            return

        path, _ = QFileDialog.getSaveFileName(self, "Save File", default_path)
        if path:
            download.setPath(path)
            download.accept()
            self.status.showMessage(f"Downloading: {os.path.basename(path)}", 4000)
        else:
            download.cancel()

    def _handle_jnlp(self, download: QWebEngineDownloadItem):
        javaws = shutil.which("javaws")
        if not javaws:
            QMessageBox.warning(
                self, "Java Web Start not found",
                "This page requires javaws.\n\n"
                "Install with:  sudo apt install icedtea-netx",
            )
            download.cancel()
            return
        tmp = os.path.join(tempfile.gettempdir(), "tls1browser_applet.jnlp")
        download.setPath(tmp)
        download.finished.connect(lambda: self._launch_javaws(javaws, tmp))
        download.accept()

    def _launch_javaws(self, javaws, path):
        self.status.showMessage("Launching Java Web Start…", 4000)
        try:
            subprocess.Popen([javaws, "-nosecurity", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            QMessageBox.critical(self, "Launch Failed", str(e))

    # ── KVM viewer launch ─────────────────────────────────────────────
    def _launch_kvm_viewer(self, params):
        """Launch the Raritan KVM viewer via CoconutAppletLauncher.

        Uses a custom Java AppletStub/JFrame wrapper that provides the
        applet runtime environment the Raritan RemoteConsoleApplet expects.
        """
        import json

        host = params.get("_connect_host", "10.1.10.36")
        port_index = params.get("_connect_pindex", "0")
        port_id = params.get("_connect_portId", "")
        port_name = params.get("_connect_pname", "")
        session_id = params.get("SESSION_ID", "")
        ssl_port = params.get("SSLPORT", "443")

        print(f"[Coconut] KVM connect: host={host} port_index={port_index} "
              f"port_id={port_id} name={port_name}", flush=True)

        # ── Locate Java (prefer JDK 11 for Applet API support) ──────
        java = self._find_java()
        if not java:
            is_linux = sys.platform.startswith("linux")
            install_hint = (
                "Install Java 11 (required for applet support):\n\n"
                "  sudo apt install openjdk-11-jdk"
            ) if is_linux else (
                "Install Java 11 with Homebrew:\n\n"
                "  brew install openjdk@11"
            )
            QMessageBox.warning(
                self, "Java Not Found",
                f"To connect to KVM targets, you need Java 11 installed.\n\n"
                f"{install_hint}\n\n"
                f"Connection details:\n"
                f"  Host: {host}:{ssl_port}\n"
                f"  Port: {port_name} (index {port_index})",
            )
            return

        javac = java.replace("/bin/java", "/bin/javac")
        if not os.path.isfile(javac):
            javac = shutil.which("javac")

        print(f"[Coconut] Using java: {java}", flush=True)

        # ── Download JARs from the Raritan device (cached) ──────────
        jar_dir = os.path.join(os.path.expanduser("~"), ".coconut", "jars")
        os.makedirs(jar_dir, exist_ok=True)

        ctx = self._get_ssl_context()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx)
        )

        jar_names = [
            "rc.jar", "rclang_en.jar", "rclang_zhs.jar",
            "rclang_zht.jar", "rclang_ja.jar",
        ]
        jar_paths = []
        for jar_name in jar_names:
            jp = os.path.join(jar_dir, jar_name)
            if not os.path.exists(jp):
                self.status.showMessage(f"Downloading {jar_name}…", 5000)
                print(f"[Coconut] Downloading {jar_name} from {host}…", flush=True)
                try:
                    with opener.open(f"https://{host}/{jar_name}") as resp:
                        with open(jp, "wb") as f:
                            f.write(resp.read())
                    print(f"[Coconut] Downloaded {jar_name} "
                          f"({os.path.getsize(jp)} bytes)", flush=True)
                except Exception as e:
                    print(f"[Coconut] Could not download {jar_name}: {e}", flush=True)
                    if jar_name == "rc.jar":
                        QMessageBox.warning(
                            self, "Download Failed",
                            f"Could not download {jar_name} from {host}:\n{e}",
                        )
                        return
            if os.path.exists(jp):
                jar_paths.append(jp)

        # ── Compile launcher if needed ──────────────────────────────
        launcher_src = os.path.join(os.path.dirname(__file__),
                                    "CoconutAppletLauncher.java")
        launcher_cls = os.path.join(jar_dir, "CoconutAppletLauncher.class")

        if not os.path.exists(launcher_cls) or (
            os.path.exists(launcher_src) and
            os.path.getmtime(launcher_src) > os.path.getmtime(launcher_cls)
        ):
            if not javac:
                QMessageBox.warning(
                    self, "javac Not Found",
                    "Need javac to compile the applet launcher.\n"
                    "Install a full JDK (not just JRE).",
                )
                return
            print(f"[Coconut] Compiling CoconutAppletLauncher…", flush=True)
            r = subprocess.run(
                [javac, "-source", "11", "-target", "11",
                 "-d", jar_dir, launcher_src],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(f"[Coconut] javac failed: {r.stderr}", flush=True)
                QMessageBox.critical(self, "Compile Failed", r.stderr)
                return
            print("[Coconut] Compilation OK", flush=True)

        # ── Build applet parameter dict ─────────────────────────────
        applet_params = {
            "java_arguments": "-Xmx512m -Dsun.java2d.noddraw=true",
            "progressbar": "true",
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
            "PLAY_AUDIO": params.get("PLAY_AUDIO", ""),
            "LANGUAGE": params.get("LANGUAGE", "en"),
            "InFrame": "no",
            "CONNECT_PORT_NAME": port_name,
            "CONNECT_INDEX": str(port_index),
            "CONNECT_PORT_ID": port_id,
            "CONNECT_PORT_TYPE": params.get("_connect_ptype", "Dual-VM"),
        }

        print(f"[Coconut] Applet params: {json.dumps(applet_params, indent=2)}",
              flush=True)

        # ── Build launch command ────────────────────────────────────
        sep = ";" if sys.platform == "win32" else ":"
        classpath = sep.join([jar_dir] + jar_paths)
        codebase_url = f"https://{host}/"

        security_file = os.path.join(os.path.dirname(__file__),
                                     "coconut.java.security")

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

        print(f"[Coconut] Command: {' '.join(cmd[:8])}…", flush=True)
        self.status.showMessage(f"Launching KVM viewer for {port_name}…", 5000)

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                cwd=jar_dir,
            )
            print(f"[Coconut] KVM viewer launched (PID {proc.pid})", flush=True)
        except Exception as e:
            print(f"[Coconut] Failed to launch Java: {e}", flush=True)
            QMessageBox.critical(self, "Launch Failed", str(e))

    # ── Proxy ─────────────────────────────────────────────────────────
    def _auto_start_proxy(self):
        try:
            self._proxy.start()
            self.proxy_btn.setChecked(True)
            url = self._proxy.url
            self.proxy_btn.setText("  Proxy ON  ")
            self.proxy_label.setText(f"  ●  Proxy ON — {url}")
            self.proxy_label.setStyleSheet(
                "color: #4ade80; font-weight: bold; font-size: 12px; padding: 0 12px;")
        except Exception as e:
            print(f"[Coconut] Proxy auto-start failed: {e}", flush=True)

    def _toggle_proxy(self):
        if self._proxy.running:
            self._proxy.stop()
            self.proxy_btn.setChecked(False)
            self.proxy_btn.setText("  Proxy OFF  ")
            self.proxy_label.setText("  ○  Proxy OFF")
            self.proxy_label.setStyleSheet(
                f"color: {TEXT_SECONDARY}; font-size: 12px; padding: 0 12px;")
            self.status.showMessage("TLS Proxy stopped", 3000)
        else:
            try:
                self._proxy.start()
                self.proxy_btn.setChecked(True)
                url = self._proxy.url
                self.proxy_btn.setText(f"  Proxy ON  ")
                self.proxy_label.setText(f"  ●  Proxy ON — {url}")
                self.proxy_label.setStyleSheet(
                    "color: #4ade80; font-weight: bold; font-size: 12px; padding: 0 12px;")
                self.status.showMessage(
                    f"TLS Proxy running — other computers can access {url}", 6000)
            except Exception as e:
                self.proxy_btn.setChecked(False)
                QMessageBox.warning(self, "Proxy Error",
                                    f"Failed to start proxy:\n{e}")

    def _on_kvm_disconnect(self):
        print("[Coconut] KVM viewer disconnected", flush=True)
        if hasattr(self, '_kvm_widget') and self._kvm_widget:
            self._kvm_widget.cleanup()
            self._kvm_widget.hide()
            self._kvm_widget.deleteLater()
            self._kvm_widget = None
        v = self._current_view()
        if v:
            host = getattr(self, "_kvm_host", "10.1.10.36")
            v.setUrl(QUrl(f"https://{host}/"))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_kvm_widget') and self._kvm_widget:
            self._kvm_widget.setGeometry(self.rect())

    def _find_java(self):
        """Find a Java binary, preferring JDK 11 (has Applet API)."""
        candidates = [
            "/opt/homebrew/opt/openjdk@11/bin/java",
            "/usr/lib/jvm/java-11-openjdk-amd64/bin/java",
            "/usr/lib/jvm/java-11-openjdk/bin/java",
            "/usr/lib/jvm/java-8-openjdk-amd64/bin/java",
            "/usr/lib/jvm/java-8-openjdk/bin/java",
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c

        java = shutil.which("java")
        if java and os.path.realpath(java) not in ("/usr/bin/java",):
            return java

        if java:
            try:
                r = subprocess.run(
                    [java, "-version"], capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and "Unable to locate" not in r.stderr:
                    return java
            except Exception:
                pass
        return None

    def _get_ssl_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.options &= ~ssl.OP_NO_TLSv1
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, ssl.SSLError):
            pass
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            ctx.set_ciphers("ALL")
        return ctx

    # ── Zoom ────────────────────────────────────────────────────────────
    def _zoom_in(self):
        v = self._current_view()
        if v: v.setZoomFactor(v.zoomFactor() + 0.1)

    def _zoom_out(self):
        v = self._current_view()
        if v: v.setZoomFactor(max(0.25, v.zoomFactor() - 0.1))

    def _zoom_reset(self):
        v = self._current_view()
        if v: v.setZoomFactor(1.0)

    def _show_about(self):
        proxy_info = ""
        if self._proxy.running:
            proxy_info = f"<b>Proxy:</b> {self._proxy.url}<br>"
        QMessageBox.about(
            self, "About Coconut",
            "<h2>Coconut</h2>"
            "<p>Dedicated browser for Raritan and legacy Java-applet equipment.</p>"
            "<p><b>Engine:</b> Chromium (QtWebEngine)<br>"
            "<b>JavaScript:</b> V8<br>"
            "<b>Java:</b> Emulated (applet stubs)<br>"
            f"<b>TLS:</b> 1.0 – 1.3<br>{proxy_info}</p>",
        )

    def closeEvent(self, event):
        if self._proxy.running:
            self._proxy.stop()
        super().closeEvent(event)


def main():
    _configure_global_ssl()

    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(_CHROMIUM_BASE_FLAGS)

    app = QApplication(sys.argv)
    app.setApplicationName("Coconut")
    app.setOrganizationName("Coconut")
    app.setStyleSheet(STYLESHEET)

    print("[Coconut] Starting — deployJava setter trap + applet stubs active", flush=True)

    window = BrowserWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
