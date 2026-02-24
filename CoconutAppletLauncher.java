import java.applet.Applet;
import java.applet.AppletContext;
import java.applet.AppletStub;
import java.applet.AudioClip;
import java.awt.*;
import java.awt.event.*;
import java.io.*;
import java.lang.reflect.Method;
import java.net.URL;
import java.security.cert.X509Certificate;
import java.util.*;
import javax.net.ssl.*;
import javax.swing.*;

@SuppressWarnings("deprecation")
public class CoconutAppletLauncher extends JFrame implements AppletStub, AppletContext {

    private Applet applet;
    private final Map<String, String> params;
    private final URL codeBase;
    private final URL documentBase;
    private volatile boolean active = true;
    private final boolean bridgeMode;
    private final boolean embedMode;

    private int captureW = 1024, captureH = 768;

    public CoconutAppletLauncher(String className, URL codeBase,
                                  Map<String, String> params,
                                  boolean bridge, boolean embed) throws Exception {
        this.params = params;
        this.codeBase = codeBase;
        this.documentBase = codeBase;
        this.bridgeMode = bridge;
        this.embedMode = embed;

        log("Loading applet class: " + className);
        log("Codebase: " + codeBase);
        log("Mode: " + (embed ? "embed" : bridge ? "bridge" : "standalone"));
        for (Map.Entry<String, String> e : params.entrySet())
            log("  " + e.getKey() + " = " + e.getValue());

        Class<?> cls = Class.forName(className);
        applet = (Applet) cls.getDeclaredConstructor().newInstance();
        applet.setStub(this);

        setTitle("CoconutKVM");
        setDefaultCloseOperation(JFrame.DISPOSE_ON_CLOSE);
        addWindowListener(new WindowAdapter() {
            @Override public void windowClosing(WindowEvent e) { shutdown(); }
        });

        if (embed) {
            // Embed mode: small window, no decorations, no maximize.
            // Python will reparent this into its own widget.
            setUndecorated(true);
            setResizable(true);
            setSize(captureW, captureH);
            applet.setPreferredSize(new Dimension(captureW, captureH));

            // Remove the applet's own menu bar to save vertical space
            setJMenuBar(null);
        } else {
            // Standalone mode: fullscreen, undecorated
            setUndecorated(true);
            setExtendedState(JFrame.MAXIMIZED_BOTH);
            applet.setPreferredSize(new Dimension(captureW, captureH));
            installScrollLockListener();
        }

        setLayout(new BorderLayout());
        add(applet, BorderLayout.CENTER);
        setVisible(true);

        log("Calling applet.init()...");
        applet.init();
        log("Calling applet.start()...");
        applet.start();
        log("Applet started, showing=" + applet.isShowing()
                + " size=" + applet.getWidth() + "x" + applet.getHeight());

        // Try to hide the applet's internal toolbar/menubar for cleaner embed
        if (embed) {
            hideAppletToolbars();
        }

        scheduleConnect();
    }

    private void hideAppletToolbars() {
        SwingUtilities.invokeLater(() -> {
            try {
                // Remove any JMenuBar the applet might have added to us
                if (getJMenuBar() != null) {
                    setJMenuBar(null);
                }
                // Look for toolbar-like components inside the applet
                hideToolbarsRecursive(applet);
                revalidate();
                repaint();
            } catch (Exception e) {
                log("Toolbar hide: " + e.getMessage());
            }
        });
    }

    private void hideToolbarsRecursive(Container c) {
        for (Component comp : c.getComponents()) {
            if (comp instanceof JToolBar || comp instanceof JMenuBar) {
                comp.setVisible(false);
                log("Hidden toolbar: " + comp.getClass().getName());
            }
            if (comp instanceof Container) {
                hideToolbarsRecursive((Container) comp);
            }
        }
    }

    // ── Bridge: events via stdin ──────────────────────────────────────

    private void startBridge() {
        Thread receiver = new Thread(this::eventReceiverLoop, "EventReceiver");
        receiver.setDaemon(true);
        receiver.start();
    }

    private void eventReceiverLoop() {
        DataInputStream in = new DataInputStream(
                new BufferedInputStream(System.in));
        while (active) {
            try {
                int type = in.readByte() & 0xFF;
                switch (type) {
                    case 'M': case 'D': case 'P': case 'L': {
                        int x = in.readUnsignedShort();
                        int y = in.readUnsignedShort();
                        int btn = in.readByte() & 0xFF;
                        int id;
                        switch (type) {
                            case 'P': id = MouseEvent.MOUSE_PRESSED; break;
                            case 'L': id = MouseEvent.MOUSE_RELEASED; break;
                            case 'D': id = MouseEvent.MOUSE_DRAGGED; break;
                            default:  id = MouseEvent.MOUSE_MOVED; break;
                        }
                        final int fid = id, fx = x, fy = y, fb = btn;
                        SwingUtilities.invokeLater(() -> dispatchMouse(fid, fx, fy, fb));
                        break;
                    }
                    case 'K': {
                        int kc = in.readInt();
                        int down = in.readByte() & 0xFF;
                        int mods = in.readByte() & 0xFF;
                        final int fkc = kc, fdown = down, fmods = mods;
                        SwingUtilities.invokeLater(() ->
                            dispatchKey(fdown == 1 ? KeyEvent.KEY_PRESSED
                                                   : KeyEvent.KEY_RELEASED, fkc, fmods));
                        break;
                    }
                    case 'R': {
                        captureW = in.readUnsignedShort();
                        captureH = in.readUnsignedShort();
                        SwingUtilities.invokeLater(() -> {
                            applet.setPreferredSize(new Dimension(captureW, captureH));
                            applet.setSize(captureW, captureH);
                            setSize(captureW, captureH);
                            validate();
                        });
                        break;
                    }
                    case 'Q': {
                        shutdown();
                        return;
                    }
                }
            } catch (EOFException e) {
                log("Input pipe closed");
                shutdown();
                return;
            } catch (Exception e) {
                if (active) log("Event error: " + e.getMessage());
                break;
            }
        }
    }

    private void dispatchMouse(int id, int x, int y, int btn) {
        int awtBtn = MouseEvent.NOBUTTON;
        int mods = 0;
        if ((btn & 1) != 0) { awtBtn = MouseEvent.BUTTON1; mods |= InputEvent.BUTTON1_DOWN_MASK; }
        if ((btn & 2) != 0) { awtBtn = MouseEvent.BUTTON3; mods |= InputEvent.BUTTON3_DOWN_MASK; }
        if ((btn & 4) != 0) { awtBtn = MouseEvent.BUTTON2; mods |= InputEvent.BUTTON2_DOWN_MASK; }

        Component target = applet;
        Component deep = SwingUtilities.getDeepestComponentAt(applet, x, y);
        if (deep != null) {
            Point p = SwingUtilities.convertPoint(applet, x, y, deep);
            x = p.x; y = p.y;
            target = deep;
        }

        MouseEvent evt = new MouseEvent(target, id,
                System.currentTimeMillis(), mods, x, y, 1, false, awtBtn);
        target.dispatchEvent(evt);
    }

    private void dispatchKey(int id, int keyCode, int modMask) {
        int mods = 0;
        if ((modMask & 1) != 0) mods |= InputEvent.SHIFT_DOWN_MASK;
        if ((modMask & 2) != 0) mods |= InputEvent.CTRL_DOWN_MASK;
        if ((modMask & 4) != 0) mods |= InputEvent.ALT_DOWN_MASK;
        if ((modMask & 8) != 0) mods |= InputEvent.META_DOWN_MASK;

        char keyChar = KeyEvent.CHAR_UNDEFINED;
        if (keyCode >= 32 && keyCode < 127) keyChar = (char) keyCode;

        KeyEvent evt = new KeyEvent(applet, id,
                System.currentTimeMillis(), mods, keyCode, keyChar);
        applet.dispatchEvent(evt);

        if (id == KeyEvent.KEY_PRESSED && keyChar != KeyEvent.CHAR_UNDEFINED) {
            KeyEvent typed = new KeyEvent(applet, KeyEvent.KEY_TYPED,
                    System.currentTimeMillis(), mods, KeyEvent.VK_UNDEFINED, keyChar);
            applet.dispatchEvent(typed);
        }
    }

    // ── Standalone mode: Scroll Lock listener ────────────────────────

    private long lastScrollLock = 0;

    private void installScrollLockListener() {
        KeyboardFocusManager.getCurrentKeyboardFocusManager()
            .addKeyEventDispatcher(e -> {
                if (e.getID() == KeyEvent.KEY_PRESSED
                        && e.getKeyCode() == KeyEvent.VK_SCROLL_LOCK) {
                    long now = System.currentTimeMillis();
                    if (now - lastScrollLock < 600) {
                        log("Double Scroll Lock — exiting");
                        SwingUtilities.invokeLater(this::shutdown);
                        return true;
                    }
                    lastScrollLock = now;
                }
                return false;
            });
    }

    // ── Auto-connect ─────────────────────────────────────────────────

    private void scheduleConnect() {
        String portId = params.get("CONNECT_PORT_ID");
        String portName = params.get("CONNECT_PORT_NAME");
        String portIndex = params.getOrDefault("CONNECT_INDEX", "0");
        if (portId == null || portId.isEmpty()) return;

        log("Auto-connect to " + portName + " in 2s...");
        new Thread(() -> {
            try { Thread.sleep(2000); } catch (InterruptedException ignored) {}
            SwingUtilities.invokeLater(() -> {
                try {
                    Method m = findMethod("connect");
                    if (m == null) { log("No connect() method"); return; }
                    Class<?>[] pt = m.getParameterTypes();
                    String ptype = params.getOrDefault("CONNECT_PORT_TYPE", "Dual-VM");
                    Object[] args = pt.length == 7
                        ? new Object[]{0, "0", portIndex, portId, portName, ptype, "CCC"}
                        : new Object[]{0, portIndex, portId, portName, ptype, "CCC"};
                    log("Calling connect()...");
                    m.invoke(applet, args);
                    log("connect() OK");
                } catch (Exception e) {
                    log("connect() failed: " + e.getMessage());
                }
            });
        }, "AutoConnect").start();
    }

    private Method findMethod(String name) {
        for (Method m : applet.getClass().getMethods())
            if (name.equals(m.getName())) return m;
        return null;
    }

    // ── Lifecycle ────────────────────────────────────────────────────

    private void shutdown() {
        if (!active) return;
        active = false;
        try { applet.stop(); } catch (Exception ignored) {}
        try { applet.destroy(); } catch (Exception ignored) {}
        dispose();
        log("Shutdown complete");
        System.exit(0);
    }

    // ── AppletStub ───────────────────────────────────────────────────
    @Override public boolean isActive() { return active; }
    @Override public URL getDocumentBase() { return documentBase; }
    @Override public URL getCodeBase() { return codeBase; }
    @Override public String getParameter(String name) { return params.get(name); }
    @Override public AppletContext getAppletContext() { return this; }
    @Override public void appletResize(int w, int h) { setSize(w, h); validate(); }

    // ── AppletContext ────────────────────────────────────────────────
    @Override public AudioClip getAudioClip(URL url) { return null; }
    @Override public Image getImage(URL url) { return Toolkit.getDefaultToolkit().getImage(url); }
    @Override public Applet getApplet(String name) { return applet; }
    @Override public Enumeration<Applet> getApplets() {
        return Collections.enumeration(Collections.singletonList(applet));
    }
    @Override public void showDocument(URL url) { log("showDocument: " + url); }
    @Override public void showDocument(URL url, String t) { log("showDocument: " + url); }
    @Override public void showStatus(String s) { log("Status: " + s); }
    @Override public void setStream(String k, InputStream s) {}
    @Override public InputStream getStream(String k) { return null; }
    @Override public Iterator<String> getStreamKeys() { return Collections.emptyIterator(); }

    // ── Utilities ────────────────────────────────────────────────────

    private static void log(String msg) {
        System.err.println("[Coconut] " + msg);
        System.err.flush();
    }

    private static void installTrustAll() {
        try {
            TrustManager[] tm = { new X509TrustManager() {
                public X509Certificate[] getAcceptedIssuers() { return null; }
                public void checkClientTrusted(X509Certificate[] c, String a) {}
                public void checkServerTrusted(X509Certificate[] c, String a) {}
            }};
            SSLContext sc = SSLContext.getInstance("TLS");
            sc.init(null, tm, new java.security.SecureRandom());
            HttpsURLConnection.setDefaultSSLSocketFactory(sc.getSocketFactory());
            HttpsURLConnection.setDefaultHostnameVerifier((h, s) -> true);
        } catch (Exception e) { log("Trust-all failed: " + e); }
    }

    // ── Main ─────────────────────────────────────────────────────────

    public static void main(String[] args) {
        if (args.length < 2) {
            System.err.println("Usage: CoconutAppletLauncher <class> <codebase> [--bridge|--embed] [k=v ...]");
            System.exit(1);
        }

        installTrustAll();
        RepaintManager.currentManager(null).setDoubleBufferingEnabled(true);

        String className = args[0];
        String codebaseStr = args[1];
        boolean bridge = false;
        boolean embed = false;
        Map<String, String> params = new LinkedHashMap<>();

        for (int i = 2; i < args.length; i++) {
            if ("--bridge".equals(args[i])) { bridge = true; continue; }
            if ("--embed".equals(args[i])) { embed = true; continue; }
            int eq = args[i].indexOf('=');
            if (eq > 0) params.put(args[i].substring(0, eq), args[i].substring(eq + 1));
        }

        final boolean fBridge = bridge;
        final boolean fEmbed = embed;
        SwingUtilities.invokeLater(() -> {
            try {
                new CoconutAppletLauncher(className, new URL(codebaseStr), params, fBridge, fEmbed);
            } catch (Exception e) {
                log("Fatal: " + e.getMessage());
                e.printStackTrace();
                System.exit(1);
            }
        });
    }
}
