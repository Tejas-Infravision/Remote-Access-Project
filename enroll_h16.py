#!/usr/bin/env python3
"""
H16 Hand Controller Fleet Enrollment Script
Run on a Windows technician laptop with the H16 plugged in via USB.
Requires: frida Python package (pip install frida), frida-server-*-android-arm64 binary in script dir.
"""

import os
import subprocess
import sys
import re
import time
import datetime
import threading
import queue
from pathlib import Path

# Prevent Git Bash / MSYS from corrupting Android-side paths in ADB arguments
os.environ["MSYS_NO_PATHCONV"] = "1"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_FROZEN    = getattr(sys, "frozen", False)
SCRIPT_DIR = Path(sys.executable).parent if _FROZEN else Path(__file__).parent
BUNDLE_DIR = Path(sys._MEIPASS) if (_FROZEN and hasattr(sys, "_MEIPASS")) else Path(__file__).parent

LOG_FILE   = SCRIPT_DIR / "enrollment_log.txt"

# r33 ADB at the fixed path — must be 1.0.41; v37.x writes 0 bytes to H16 via USB push
ADB_PATH   = Path(r"C:\platform-tools-old\adb.exe")

TAILSCALE_PKG      = "com.tailscale.ipn"
TAILSCALE_ACTIVITY = "com.tailscale.ipn/.IPNActivity"

FRIDA_REMOTE = "/data/local/tmp/frida-server"
FRIDA_PORT   = 27042

RUSTDESK_PKG    = "com.carriez.flutter_hbb"
RUSTDESK_APK    = BUNDLE_DIR / "apks" / "rustdesk.apk"
RUSTDESK_REMOTE = "/sdcard/rustdesk.apk"

# ADB target — mutated by set_adb_target()
_adb_target: list[str] = []

# ---------------------------------------------------------------------------
# Logging and output helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def fatal(msg: str) -> None:
    print(f"\n[ERROR] {msg}")
    _log(f"ERROR: {msg}")
    sys.exit(1)

def step(msg: str) -> None:
    print(f"\n>>> {msg}")
    _log(f"STEP: {msg}")

def info(msg: str) -> None:
    print(f"    {msg}")

def technician_prompt(msg: str) -> None:
    print(f"\n[ACTION REQUIRED] {msg}")
    input("    Press Enter when done > ")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict[str, str]:
    path = SCRIPT_DIR / "config.env"
    if not path.exists():
        fatal(
            f"config.env not found. Expected at:\n  {path}\n"
            "Make sure you are running this script from the fleet-enrollment folder."
        )
    cfg: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip()
    return cfg

# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def set_adb_target(serial: str | None) -> None:
    global _adb_target
    _adb_target = ["-s", serial] if serial else []

def adb_run(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    cmd = [str(ADB_PATH)] + _adb_target + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ADB timed out after {timeout}s: adb {' '.join(args)}")
    except Exception as e:
        raise RuntimeError(f"ADB execution error: {e}")

def adb_ok(*args: str, timeout: int = 30, hint: str = "") -> str:
    rc, out, err = adb_run(*args, timeout=timeout)
    if rc != 0:
        detail = (err or out).strip()
        raise RuntimeError(
            f"adb {' '.join(args)} failed (exit {rc}): {detail}"
            + (f"\n  Hint: {hint}" if hint else "")
        )
    return out

def is_package_installed(pkg: str) -> bool:
    rc, out, _ = adb_run("shell", "pm", "list", "packages")
    return pkg in out

# ---------------------------------------------------------------------------
# Tailscale startup
# ---------------------------------------------------------------------------

def start_tailscale_app() -> None:
    """Launch Tailscale foreground activity so the IPN backend initialises."""
    step("Starting Tailscale app (am start)")
    rc, out, err = adb_run("shell", "am", "start", "-n", TAILSCALE_ACTIVITY)
    if rc != 0:
        info(f"  am start returned non-zero ({err.strip()}) — app may already be running.")
    else:
        info("  Tailscale activity started.")
    # Give the IPN backend time to reach NeedsLogin state before we inject
    time.sleep(4)

# ---------------------------------------------------------------------------
# Logcat monitor for Tailscale IPN state transitions
# ---------------------------------------------------------------------------

def start_logcat_monitor() -> threading.Event:
    """
    Background thread watching logcat for the Tailscale state transition that
    confirms a successful auth key login: 'Switching ipn state NeedsLogin -> Running'.
    Returns an Event that is set when the transition is observed.
    """
    success_event = threading.Event()

    def _monitor() -> None:
        cmd = [str(ADB_PATH)] + list(_adb_target) + ["logcat", "-v", "brief"]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                if "NeedsLogin -> Running" in line:
                    _log(f"Logcat: {line.strip()}")
                    success_event.set()
                    break
                # Also catch the generic "ipn state" prefix in case wording differs
                if "ipn state" in line and "Running" in line:
                    _log(f"Logcat (state): {line.strip()}")
                    success_event.set()
                    break
            proc.terminate()
        except Exception as e:
            _log(f"Logcat monitor error: {e}")

    t = threading.Thread(target=_monitor, daemon=True, name="logcat-monitor")
    t.start()
    return success_event

# ---------------------------------------------------------------------------
# Frida JavaScript — injected into com.tailscale.ipn to call auth key login
# ---------------------------------------------------------------------------
# Uses __AUTH_KEY__ as a placeholder; replaced at runtime before injection.
# Strategy:
#   1. Enumerate all loaded com.tailscale.ipn classes and find methods that
#      accept a single String argument and have auth/login/key in their name.
#   2. Try each candidate: first as a static call, then via the Application
#      instance obtained from ActivityThread.
#   3. If no Java method works, enumerate JNI exports from libgio.so (the Go
#      backend shared library) and report native candidates for manual follow-up.

_FRIDA_JS = r"""
"use strict";
var AUTH_KEY = "__AUTH_KEY__";

Java.perform(function () {
    send({ type: "status", msg: "Frida attached" });

    var authCalled = false;

    function tryCall(cls, mname) {
        if (authCalled) return;
        // Static call
        try {
            cls[mname](AUTH_KEY);
            send({ type: "success", cls: cls.$className, method: mname, style: "static" });
            authCalled = true;
            return;
        } catch (_) {}
        // Instance call via ActivityThread
        try {
            var AT  = Java.use("android.app.ActivityThread");
            var app = AT.currentApplication();
            Java.cast(app, cls)[mname](AUTH_KEY);
            send({ type: "success", cls: cls.$className, method: mname, style: "instance" });
            authCalled = true;
        } catch (_) {}
    }

    Java.enumerateLoadedClasses({
        onMatch: function (name) {
            if (authCalled) return;
            if (!name.startsWith("com.tailscale.ipn")) return;
            try {
                var cls = Java.use(name);
                cls.class.getDeclaredMethods().forEach(function (m) {
                    if (authCalled) return;
                    var params = m.getParameterTypes();
                    if (params.length !== 1) return;
                    if (params[0].getName() !== "java.lang.String") return;
                    var mname = m.getName();
                    send({ type: "method", cls: name, method: mname });
                    if (/auth|login|key/i.test(mname)) tryCall(cls, mname);
                });
            } catch (_) {}
        },
        onComplete: function () {
            if (!authCalled) {
                // Report native JNI exports from the Go backend for manual analysis
                try {
                    Process.getModuleByName("libgio.so")
                        .enumerateExports()
                        .filter(function (e) {
                            return e.type === "function" &&
                                   e.name.startsWith("Java_com_tailscale") &&
                                   /auth|login|key/i.test(e.name);
                        })
                        .forEach(function (e) {
                            send({ type: "native", name: e.name, addr: e.address });
                        });
                } catch (e) {
                    send({ type: "native_error", err: e.message });
                }
            }
            send({ type: "done", success: authCalled });
        }
    });
});
"""

# ---------------------------------------------------------------------------
# Frida auth
# ---------------------------------------------------------------------------

def _find_frida_server_binary() -> Path | None:
    """Look for a frida-server ARM64 binary next to the script/exe."""
    candidates = [
        "frida-server",
        "frida-server-arm64",
    ]
    # Also accept versioned names like frida-server-16.1.4-android-arm64
    for p in SCRIPT_DIR.iterdir():
        if p.is_file() and p.name.startswith("frida-server") and "arm64" in p.name:
            return p
    for name in candidates:
        p = SCRIPT_DIR / name
        if p.exists():
            return p
    return None


def frida_auth(auth_key: str) -> bool:
    """
    Push frida-server ARM64, start it as root, inject the Tailscale auth key
    via Java method enumeration.  Returns True if a login method was called.
    """
    try:
        import frida  # type: ignore
    except ImportError:
        info("  frida Python module not installed — run: pip install frida")
        info("  Skipping Frida auth approach.")
        return False

    frida_local = _find_frida_server_binary()
    if frida_local is None:
        info("  frida-server binary not found in script directory.")
        info("  Download frida-server-*-android-arm64 from:")
        info("  https://github.com/frida/frida/releases")
        info("  Place it next to this script and re-run.")
        return False

    info(f"  Using frida-server binary: {frida_local.name}")

    # Push frida-server (over WiFi ADB — USB push writes 0 bytes on H16)
    step("Pushing frida-server to /data/local/tmp/")
    try:
        adb_ok("push", str(frida_local), FRIDA_REMOTE, timeout=120)
    except RuntimeError as e:
        info(f"  Push failed: {e}")
        return False

    rc, out, _ = adb_run("shell", "stat", "-c", "%s", FRIDA_REMOTE)
    try:
        sz = int(out.strip())
    except ValueError:
        sz = 0
    if sz == 0:
        info("  frida-server arrived as 0 bytes — WiFi ADB push may not be active.")
        return False
    info(f"  frida-server on device: {sz:,} bytes")

    # chmod + kill any stale instance + start fresh, all as root
    # NOTE: su -c does not work on this firmware; use "su root <cmd>" syntax
    step("Starting frida-server as root")
    adb_run("shell", "su", "root", "chmod", "+x", FRIDA_REMOTE)
    adb_run("shell", "su", "root", "killall", "frida-server")
    time.sleep(1)

    # Fire-and-forget: start frida-server in background via a single shell string
    # so the shell interprets '&'
    subprocess.Popen(
        [str(ADB_PATH)] + list(_adb_target) + [
            "shell",
            f"su root {FRIDA_REMOTE} > /data/local/tmp/frida-server.log 2>&1 &"
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    info("  Waiting for frida-server to initialise...")
    time.sleep(4)

    # Forward the Frida port so we can reach it from localhost
    step(f"Forwarding Frida port {FRIDA_PORT}")
    try:
        adb_ok("forward", f"tcp:{FRIDA_PORT}", f"tcp:{FRIDA_PORT}")
    except RuntimeError as e:
        info(f"  Port forwarding failed: {e}")
        return False

    # Connect to frida-server via the forwarded port
    step("Connecting Frida client to device")
    try:
        dev = frida.get_device_manager().add_remote_device(f"localhost:{FRIDA_PORT}")
    except Exception as e:
        info(f"  Could not reach frida-server: {e}")
        info("  Check /data/local/tmp/frida-server.log on the device for errors.")
        return False

    # Attach to the Tailscale process
    step("Attaching to com.tailscale.ipn process")
    session = None
    for proc_name in (TAILSCALE_PKG, "com.tailscale.ipn.android"):
        try:
            session = dev.attach(proc_name)
            info(f"  Attached to process: {proc_name}")
            break
        except Exception:
            pass
    if session is None:
        # Last resort: search by name in process list
        try:
            procs = dev.enumerate_processes()
            for p in procs:
                if "tailscale" in p.name.lower():
                    session = dev.attach(p.pid)
                    info(f"  Attached to process: {p.name} (pid {p.pid})")
                    break
        except Exception:
            pass
    if session is None:
        info("  Tailscale process not found — ensure the app is running on the H16.")
        adb_run("forward", "--remove", f"tcp:{FRIDA_PORT}")
        return False

    # Inject script
    js = _FRIDA_JS.replace("__AUTH_KEY__", auth_key.replace('"', '\\"'))
    result: dict = {"success": False, "native_candidates": []}
    done_event = threading.Event()

    def on_message(message: dict, _data: object) -> None:
        if message.get("type") != "send":
            return
        p = message.get("payload", {})
        t = p.get("type", "")
        if t == "success":
            info(f"  Auth method called: {p.get('cls')}.{p.get('method')} [{p.get('style')}]")
            _log(f"Frida auth called: {p.get('cls')}.{p.get('method')}")
            result["success"] = True
        elif t == "method":
            info(f"  Found method: {p.get('cls')}.{p.get('method')}")
        elif t == "native":
            info(f"  Native JNI candidate: {p.get('name')}")
            result["native_candidates"].append(p.get("name"))
        elif t == "native_error":
            info(f"  libgio.so enumeration error: {p.get('err')}")
        elif t == "status":
            info(f"  {p.get('msg')}")
        elif t == "done":
            done_event.set()

    script = session.create_script(js)
    script.on("message", on_message)
    script.load()

    info("  Running class enumeration (up to 30s)...")
    done_event.wait(timeout=30)

    try:
        script.unload()
        session.detach()
    except Exception:
        pass
    adb_run("forward", "--remove", f"tcp:{FRIDA_PORT}")

    if result["native_candidates"] and not result["success"]:
        info("  No Java auth method found, but these native JNI exports may be relevant:")
        for name in result["native_candidates"]:
            info(f"    {name}")
        info("  Manual Frida scripting against these exports may be required.")

    return result["success"]

# ---------------------------------------------------------------------------
# EncryptedSharedPreferences fallback (Android 7, software-backed keystore)
# ---------------------------------------------------------------------------

def sharedprefs_fallback(auth_key: str) -> None:
    """
    Pull the EncryptedSharedPreferences XML and the Android Keystore entries to
    the technician laptop for offline analysis.  Actual decryption/re-encryption
    is not automated here because it requires knowledge of the specific key
    wrapping used by this build; this step saves the artefacts needed to do it.
    """
    info("Attempting SharedPreferences fallback (pulling artefacts for offline analysis)...")

    TS_PREFS    = "/data/data/com.tailscale.ipn/shared_prefs/secret_shared_prefs.xml"
    KEYSTORE_DIR = "/data/misc/keystore/user_0"

    # Force-stop to release file locks (su root syntax — not su -c)
    adb_run("shell", "su", "root", "am", "force-stop", TAILSCALE_PKG)
    time.sleep(2)

    local_prefs = SCRIPT_DIR / "secret_shared_prefs.xml"
    local_ks    = SCRIPT_DIR / "keystore_dump"

    rc, _, err = adb_run("pull", TS_PREFS, str(local_prefs), timeout=15)
    if rc != 0 or not local_prefs.exists() or local_prefs.stat().st_size == 0:
        info(f"  Could not pull prefs file ({err.strip()}) — SharedPreferences fallback unavailable.")
        return

    local_ks.mkdir(exist_ok=True)
    adb_run("pull", KEYSTORE_DIR, str(local_ks), timeout=30)

    info("  Artefacts pulled for offline analysis:")
    info(f"    {local_prefs}")
    info(f"    {local_ks}")
    info("  Decrypt with the AES master key from keystore_dump, modify the auth state entry,")
    info("  re-encrypt, push back, and restart the Tailscale app.")
    _log("SharedPreferences fallback: artefacts pulled for manual analysis.")

# ---------------------------------------------------------------------------
# Wait for logcat auth confirmation
# ---------------------------------------------------------------------------

def wait_for_auth(event: threading.Event, timeout_sec: int = 120) -> bool:
    step(f"Waiting for Tailscale to reach Running state (up to {timeout_sec}s)")
    info("  Watching logcat for: 'Switching ipn state NeedsLogin -> Running'")
    deadline = time.time() + timeout_sec
    last_tick = 0.0
    while time.time() < deadline:
        if event.is_set():
            info("  Auth state transition confirmed.")
            return True
        remaining = int(deadline - time.time())
        if time.time() - last_tick >= 15:
            info(f"  Still waiting... ({remaining}s remaining)")
            last_tick = time.time()
        time.sleep(1)
    return False

# ---------------------------------------------------------------------------
# Read Tailscale IP
# ---------------------------------------------------------------------------

def read_tailscale_ip() -> str | None:
    rc, out, _ = adb_run("shell", "ip", "addr", "show", "tailscale0", timeout=10)
    m = re.search(r"inet\s+(100\.\d+\.\d+\.\d+)", out)
    if m:
        return m.group(1)
    rc2, out2, _ = adb_run("shell", "ifconfig", "tailscale0", timeout=10)
    m2 = re.search(r"inet addr[:\s]+(100\.\d+\.\d+\.\d+)", out2)
    return m2.group(1) if m2 else None

# ---------------------------------------------------------------------------
# Main enrollment flow
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("  H16 Hand Controller Fleet Enrollment")
    print("=" * 60)
    _log("=== H16 Enrollment Started ===")

    # 1. Load config
    step("Loading configuration from config.env")
    cfg = load_config()
    auth_key = cfg.get("TAILSCALE_AUTH_KEY", "")
    if not auth_key or "xxxxxx" in auth_key:
        fatal(
            "TAILSCALE_AUTH_KEY is not configured in config.env.\n"
            "Edit config.env and set a valid Tailscale pre-auth key, then run again."
        )
    info("Config loaded.")

    # 2. Verify ADB is present and is version 1.0.41 (r33)
    step(f"Verifying ADB: {ADB_PATH}")
    if not ADB_PATH.exists():
        fatal(
            f"ADB not found at {ADB_PATH}\n"
            "Ensure C:\\platform-tools-old\\ contains r33 platform tools (ADB 1.0.41).\n"
            "Do NOT use ADB v37.x — it writes 0 bytes to the H16 via USB push."
        )
    rc, out, err = adb_run("version")
    version_line = (out + err).strip().splitlines()[0] if (out + err).strip() else ""
    info(f"ADB: {version_line}")
    if "1.0.41" not in version_line:
        fatal(
            f"Wrong ADB version: {version_line}\n"
            "Requires ADB 1.0.41 (platform-tools r33). Newer versions corrupt H16 file transfers."
        )
    info("ADB 1.0.41 confirmed.")

    # 3. Detect USB-connected device
    step("Detecting H16 device via USB")
    rc, out, _ = adb_run("devices")
    usb_serial: str | None = None
    usb_state:  str | None = None
    for line in out.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and ":" not in parts[0]:   # USB serials never contain ':'
            usb_serial, usb_state = parts[0], parts[1]
            break

    if not usb_serial:
        fatal(
            "No H16 detected via USB.\n"
            "  1. Connect H16 via USB\n"
            "  2. Enable USB Debugging in H16 Developer Options\n"
            "  3. Accept the 'Allow USB Debugging' prompt on the H16 screen\n"
            "Then run this script again."
        )
    _log(f"USB device: serial={usb_serial} state={usb_state}")
    info(f"Found: {usb_serial} ({usb_state})")

    if usb_state != "device":
        hints = {
            "offline":       "Unplug and replug the USB cable, then accept the ADB prompt on H16.",
            "unauthorized":  "Tap 'Always allow from this computer' on the H16 ADB prompt.",
        }
        fatal(
            f"Device is in state '{usb_state}' — cannot proceed.\n"
            f"  {hints.get(usb_state, f'Try unplugging and reconnecting USB.')}"
        )

    set_adb_target(usb_serial)

    # 4. Read WiFi IP before switching ADB away from USB
    step("Reading device WiFi IP (wlan0)")
    rc, out, _ = adb_run("shell", "ifconfig", "wlan0")
    m = re.search(r"inet addr[:\s]+(\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    if not m:
        fatal("Could not read wlan0 IP. Ensure H16 is connected to WiFi and re-run.")
    wifi_ip = m.group(1)
    info(f"WiFi IP: {wifi_ip}")
    _log(f"WiFi IP: {wifi_ip}")

    # 5. Switch to WiFi ADB
    #    All file transfers (frida-server, RustDesk APK) must use WiFi ADB —
    #    USB push writes 0 bytes on H16 due to a firmware restriction.
    step("Switching ADB to WiFi/TCP mode (required for file transfers on H16)")
    try:
        adb_ok("tcpip", "5555")
    except RuntimeError as e:
        fatal(f"Failed to switch ADB to TCP mode: {e}")
    time.sleep(2)

    set_adb_target(None)
    info(f"Connecting to {wifi_ip}:5555 ...")
    rc, out, _ = adb_run("connect", f"{wifi_ip}:5555")
    if "connected" not in out.lower() and "already" not in out.lower():
        fatal(
            f"WiFi ADB connection failed: {out.strip()}\n"
            "Ensure this laptop and H16 are on the same WiFi network."
        )
    info(f"WiFi ADB: {out.strip()}")
    _log(f"WiFi ADB connected: {wifi_ip}:5555")

    technician_prompt(
        "WiFi ADB is now active.\n"
        "    UNPLUG the USB cable from the H16 now.\n"
        "    File transfers require WiFi ADB — USB push does not work on this device."
    )

    set_adb_target(f"{wifi_ip}:5555")
    time.sleep(2)
    rc, out, _ = adb_run("devices")
    if f"{wifi_ip}:5555" not in out:
        fatal(f"WiFi ADB to {wifi_ip}:5555 dropped. Reconnect USB and re-run.")
    info(f"WiFi ADB stable: {wifi_ip}:5555")

    # 6. Start Tailscale app so the IPN backend is in NeedsLogin state
    start_tailscale_app()

    # 7. Start logcat monitor before auth attempt
    step("Starting logcat monitor")
    logcat_event = start_logcat_monitor()
    info("  Watching for IPN state transition to Running...")

    # 8. Frida auth
    step("Authenticating Tailscale via Frida injection")
    frida_called = frida_auth(auth_key)

    if frida_called:
        info("  Login method called — waiting for IPN state change...")
    else:
        info("  Frida did not call a Java auth method.")
        info("  Attempting SharedPreferences fallback...")
        sharedprefs_fallback(auth_key)

    # 9. Wait for logcat to confirm NeedsLogin -> Running
    auth_ok = wait_for_auth(logcat_event, timeout_sec=120)

    if not auth_ok:
        # Check interface directly as a secondary confirmation
        tailscale_ip = read_tailscale_ip()
        if tailscale_ip:
            info(f"  tailscale0 interface up ({tailscale_ip}) — treating as authenticated.")
            _log(f"Auth confirmed via tailscale0 (logcat missed): {tailscale_ip}")
            auth_ok = True
        else:
            fatal(
                "Tailscale did not authenticate within 2 minutes.\n"
                "Possible causes:\n"
                "  - Auth key is expired or already consumed; generate a new ephemeral key\n"
                "  - Frida could not find the login method in this build of Tailscale v1.24.2\n"
                "  - frida-server version does not match the frida Python module version\n"
                "  - com.tailscale.ipn process was not running when Frida attached\n"
                "Manual check: adb shell logcat | grep -i 'ipn state'"
            )

    # 10. Read Tailscale IP
    step("Reading Tailscale IP from tailscale0 interface")
    tailscale_ip = read_tailscale_ip()
    if tailscale_ip:
        info(f"Tailscale IP: {tailscale_ip}")
        _log(f"Tailscale IP: {tailscale_ip}")
    else:
        info("tailscale0 IP not readable yet — check https://login.tailscale.com/admin/machines")

    # 11. Enable persistent ADB over network
    step("Enabling persistent ADB over network (port 5555)")
    try:
        # Set property then stop adbd — init restarts it automatically with TCP enabled
        # su root syntax required; su -c is not supported on this firmware
        adb_run("shell", "su", "root", "setprop", "service.adb.tcp.port", "5555", timeout=10)
        time.sleep(1)
        # Use Popen (fire-and-forget) because stopping adbd kills the current connection
        subprocess.Popen(
            [str(ADB_PATH)] + list(_adb_target) + ["shell", "su root stop adbd"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        info("  adbd restart triggered — reconnecting in 6 seconds...")
        time.sleep(6)
        # Reconnect after adbd restarts
        set_adb_target(None)
        rc, out, _ = adb_run("connect", f"{wifi_ip}:5555", timeout=15)
        if "connected" in out.lower() or "already" in out.lower():
            info("  Reconnected after adbd restart.")
        else:
            info(f"  Reconnect result: {out.strip()} (may still work momentarily)")
        set_adb_target(f"{wifi_ip}:5555")
        _log("Persistent ADB over network enabled.")
    except Exception as e:
        info(f"  Warning: ADB persistence setup issue: {e}")
        set_adb_target(f"{wifi_ip}:5555")

    # 12. Install RustDesk
    step("Installing RustDesk for remote GUI access")
    rustdesk_key     = cfg.get("RUSTDESK_KEY", "")
    ec2_tailscale_ip = cfg.get("EC2_TAILSCALE_IP", "")

    if not RUSTDESK_APK.exists():
        info("RustDesk APK not found in apks/ — skipping GUI access setup.")
        info("Place rustdesk.apk in the apks/ folder and re-run to enable GUI access.")
        _log("RustDesk skipped: APK missing")
    elif is_package_installed(RUSTDESK_PKG):
        info("RustDesk already installed.")
        _log("RustDesk already present.")
    else:
        rd_pushed = False
        for attempt in range(1, 4):
            info(f"  Push attempt {attempt}/3...")
            try:
                adb_ok("push", str(RUSTDESK_APK), RUSTDESK_REMOTE, timeout=120)
            except RuntimeError as e:
                info(f"  Push error: {e}")
                if attempt < 3:
                    time.sleep(3)
                    continue
                break
            rc2, out2, _ = adb_run("shell", "stat", "-c", "%s", RUSTDESK_REMOTE)
            try:
                sz = int(out2.strip())
            except ValueError:
                sz = 0
            if sz > 0:
                info(f"  RustDesk APK on device: {sz:,} bytes")
                _log(f"RustDesk APK pushed: {sz} bytes")
                rd_pushed = True
                break
            if attempt < 3:
                info("  APK arrived as 0 bytes — retrying...")
                time.sleep(3)

        if rd_pushed:
            adb_run(
                "shell", "am", "start",
                "-t", "application/vnd.android.package-archive",
                "-d", f"file://{RUSTDESK_REMOTE}",
                timeout=15,
            )
            technician_prompt(
                "The RustDesk installer should appear on the H16 screen.\n"
                "    Select 'Package Installer', tap 'Install', wait for 'App installed'."
            )
            if rustdesk_key and ec2_tailscale_ip and "xxxxxx" not in rustdesk_key:
                rd_uri = f"rustdesk://server?k={rustdesk_key}&r={ec2_tailscale_ip}"
                adb_run(
                    "shell", "am", "start", "-a", "android.intent.action.VIEW",
                    "-d", rd_uri, timeout=10,
                )
                info(f"  RustDesk server configured: {ec2_tailscale_ip}")
            else:
                info("  RUSTDESK_KEY not set — configure server manually in the RustDesk app.")
            _log("RustDesk installed on H16.")
        else:
            info("RustDesk APK push failed — skipping.")
            _log("RustDesk APK push failed.")

    # 13. Final output
    _log(
        f"H16 enrollment complete | wifi_ip={wifi_ip} | tailscale_ip={tailscale_ip}"
    )
    print("\n" + "=" * 60)
    print("  ENROLLMENT COMPLETE")
    print("=" * 60)
    print(f"  Tailscale IP  : {tailscale_ip or 'check login.tailscale.com/admin/machines'}")
    print(f"  WiFi ADB addr : {wifi_ip}:5555 (persistent)")
    print("  RustDesk      : Open RustDesk app on H16 to find its ID")
    print("  Device is enrolled and ready for remote management.")
    print("=" * 60)


if __name__ == "__main__":
    main()
