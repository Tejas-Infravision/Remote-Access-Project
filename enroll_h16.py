#!/usr/bin/env python3
"""
H16 Hand Controller Fleet Enrollment Script
Run on a Windows technician laptop with the H16 plugged in via USB.
"""

import subprocess
import sys
import re
import time
import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# PyInstaller --onefile extracts bundled data files to sys._MEIPASS at runtime.
# The .exe itself (and user-editable files like config.env) live next to sys.executable.
# When running as a plain .py script both paths resolve to the script's directory.

_FROZEN    = getattr(sys, "frozen", False)
SCRIPT_DIR = Path(sys.executable).parent if _FROZEN else Path(__file__).parent
BUNDLE_DIR = Path(sys._MEIPASS) if (_FROZEN and hasattr(sys, "_MEIPASS")) else Path(__file__).parent

LOG_FILE   = SCRIPT_DIR / "enrollment_log.txt"   # written next to the exe
ADB_PATH   = BUNDLE_DIR / "platform-tools-r33" / "adb.exe"  # bundled inside exe
APK_PATH   = BUNDLE_DIR / "apks" / "tailscale-1.24.2.apk"   # bundled inside exe
APK_REMOTE = "/sdcard/tailscale.apk"

TAILSCALE_PKG = "com.tailscale.ipn.android"
CHROME_PKG    = "com.android.chrome"

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
    """Direct all subsequent ADB calls to a specific device serial (or clear)."""
    global _adb_target
    _adb_target = ["-s", serial] if serial else []


def adb_run(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run an ADB command; return (returncode, stdout, stderr)."""
    cmd = [str(ADB_PATH)] + _adb_target + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ADB timed out after {timeout}s: adb {' '.join(args)}")
    except Exception as e:
        raise RuntimeError(f"ADB execution error: {e}")


def adb_ok(*args: str, timeout: int = 30, hint: str = "") -> str:
    """Run ADB command; raise RuntimeError on non-zero exit."""
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

    # 2. Verify bundled ADB version (must be 1.0.41 / platform-tools r33.0.3)
    step("Verifying ADB version (must be 1.0.41 from platform-tools r33.0.3)")
    if not ADB_PATH.exists():
        fatal(
            f"Bundled ADB not found at:\n  {ADB_PATH}\n"
            "Make sure the platform-tools-r33 folder is present next to this script.\n"
            "WARNING: Do NOT use a system-installed ADB — adb 37.x writes 0 bytes to H16."
        )
    rc, out, err = adb_run("version")
    version_line = (out + err).strip().splitlines()[0] if (out + err).strip() else ""
    info(f"ADB reports: {version_line}")
    if "1.0.41" not in version_line:
        fatal(
            f"Wrong ADB version: {version_line}\n"
            "This script requires ADB 1.0.41 (platform-tools r33.0.3).\n"
            "ADB version 37.x has a known bug that writes 0 bytes to the H16 via USB.\n"
            "Use only the bundled platform-tools-r33/adb.exe."
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
            "No H16 detected via USB ADB.\n"
            "Please:\n"
            "  1. Connect the H16 to this laptop with a USB cable\n"
            "  2. On the H16: Settings > Developer Options > enable 'USB Debugging'\n"
            "  3. On the H16: Settings > Developer Options > enable 'ADB over Network'\n"
            "  4. Accept the 'Allow USB Debugging' prompt that appears on the H16 screen\n"
            "Then run this script again."
        )
    _log(f"USB device detected: serial={usb_serial}, state={usb_state}")
    info(f"Found device: {usb_serial} (state: {usb_state})")

    # 4. Reject non-ready states
    if usb_state != "device":
        if usb_state == "offline":
            hint = "Unplug and replug the USB cable, then accept the ADB prompt on the H16 screen."
        elif usb_state == "unauthorized":
            hint = "Tap 'Always allow from this computer' on the H16 ADB prompt."
        else:
            hint = f"State is '{usb_state}'. Try unplugging and reconnecting USB."
        fatal(
            f"Device is in state '{usb_state}' — cannot proceed.\n"
            f"  {hint}\n"
            "Then run this script again."
        )

    set_adb_target(usb_serial)

    # Quick enrollment check
    step("Checking if Tailscale is already installed on this device")
    already_enrolled = is_package_installed(TAILSCALE_PKG)
    if already_enrolled:
        info("Tailscale already installed — skipping install steps.")
        _log("Tailscale already present; skipping to persistent ADB setup.")

    wifi_ip: str | None = None

    if not already_enrolled:
        # 5. Read WiFi IP before switching away from USB
        step("Reading device WiFi IP address (wlan0)")
        rc, out, _ = adb_run("shell", "ifconfig", "wlan0")
        match = re.search(r"inet addr[:\s]+(\d+\.\d+\.\d+\.\d+)", out)
        if not match:
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if not match:
            fatal(
                "Could not read wlan0 IP address from the H16.\n"
                "Please ensure the H16 is connected to a WiFi network, then run again."
            )
        wifi_ip = match.group(1)
        info(f"WiFi IP: {wifi_ip}")
        _log(f"WiFi IP: {wifi_ip}")

        # 6. Switch to WiFi ADB
        #    CRITICAL: adb push over USB writes 0 bytes on H16 due to firmware restriction.
        #    All file transfers must go over WiFi ADB.
        step("Switching ADB to WiFi mode (required — USB push writes 0 bytes on H16)")
        info("Restarting ADB daemon in TCP mode on the device...")
        try:
            adb_ok(
                "tcpip", "5555",
                hint="Make sure USB Debugging is enabled and the device is authorized."
            )
        except RuntimeError as e:
            fatal(f"Failed to switch ADB to TCP mode: {e}")
        time.sleep(2)

        # connect command does not use -s; clear target temporarily
        set_adb_target(None)
        info(f"Connecting via WiFi to {wifi_ip}:5555 ...")
        rc, out, _ = adb_run("connect", f"{wifi_ip}:5555")
        connect_out = out.strip()
        if "connected" not in connect_out.lower() and "already" not in connect_out.lower():
            fatal(
                f"WiFi ADB connection failed: {connect_out}\n"
                "Please ensure:\n"
                "  1. This laptop and the H16 are on the same WiFi network\n"
                "  2. 'ADB over Network' is enabled in H16 Developer Options\n"
                "  3. No firewall is blocking TCP port 5555"
            )
        info(f"WiFi ADB: {connect_out}")
        _log(f"WiFi ADB connected: {wifi_ip}:5555")

        # 7. Prompt to remove USB now that WiFi ADB is active
        technician_prompt(
            "WiFi ADB is now active.\n"
            "    Please UNPLUG the USB cable from the H16 now.\n"
            "    File transfers require WiFi ADB — USB push does not work on this device."
        )

        # 8. Confirm WiFi ADB is still up after USB removal
        step("Confirming WiFi ADB connection is stable after USB removal")
        set_adb_target(f"{wifi_ip}:5555")
        time.sleep(2)
        rc, out, _ = adb_run("devices")
        if f"{wifi_ip}:5555" not in out:
            fatal(
                f"WiFi ADB connection to {wifi_ip}:5555 dropped after USB removal.\n"
                "Please reconnect USB and run the script again.\n"
                "Do not remove USB until prompted."
            )
        info(f"WiFi ADB stable: {wifi_ip}:5555")

        # 9. Re-check Tailscale on WiFi connection (more reliable read)
        step("Confirming Tailscale install status via WiFi ADB")
        already_enrolled = is_package_installed(TAILSCALE_PKG)
        if already_enrolled:
            info("Tailscale already installed — skipping APK install.")
            _log("Tailscale already installed (confirmed over WiFi ADB)")
        else:
            # 10. Push APK with size verification and retry
            step("Pushing Tailscale v1.24.2 APK to device")
            info("NOTE: Standard 'adb install' fails silently on H16 — using push + am start method.")
            if not APK_PATH.exists():
                fatal(
                    f"Tailscale APK not found at:\n  {APK_PATH}\n"
                    "Place tailscale-1.24.2.apk in the apks/ folder and run again."
                )

            pushed_ok = False
            for attempt in range(1, 4):
                info(f"  Push attempt {attempt}/3...")
                try:
                    adb_ok("push", str(APK_PATH), APK_REMOTE, timeout=120)
                except RuntimeError as e:
                    info(f"  Push command error: {e}")
                    if attempt < 3:
                        info("  Retrying in 3 seconds...")
                        time.sleep(3)
                        continue
                    else:
                        fatal(
                            "APK push failed after 3 attempts.\n"
                            "Check that WiFi ADB is active and the H16 has free storage space."
                        )

                # Verify the file actually arrived with non-zero size.
                # 0-byte files indicate USB-mode push is still being used.
                rc, out, _ = adb_run("shell", "stat", "-c", "%s", APK_REMOTE)
                try:
                    size = int(out.strip())
                except ValueError:
                    size = 0

                if size > 0:
                    info(f"  APK on device: {size:,} bytes. Push succeeded.")
                    _log(f"APK pushed: {size} bytes")
                    pushed_ok = True
                    break
                else:
                    info(f"  WARNING: APK arrived as 0 bytes (attempt {attempt}/3).")
                    info("  This means USB ADB is still handling the push instead of WiFi ADB.")
                    if attempt < 3:
                        info("  Retrying in 3 seconds...")
                        time.sleep(3)
                    else:
                        fatal(
                            "APK was pushed but arrived as 0 bytes after 3 attempts.\n"
                            "Root cause: file transfers over USB ADB are blocked by H16 firmware.\n"
                            "WiFi ADB must be used. Verify that:\n"
                            "  1. 'ADB over Network' is enabled in H16 Developer Options\n"
                            "  2. The USB cable has been removed\n"
                            "  3. The WiFi ADB connection shows correctly (this laptop connected\n"
                            "     to the H16 on the same WiFi network)"
                        )

            # Install via activity manager — 'adb install' fails with silent EOF on H16
            info("Launching APK installer on H16 via activity manager...")
            rc, out, err = adb_run(
                "shell", "am", "start",
                "-t", "application/vnd.android.package-archive",
                "-d", f"file://{APK_REMOTE}",
                timeout=15,
            )
            if rc != 0:
                info(f"  am start returned non-zero ({err.strip()}) — installer may still have opened.")
                info("  Check the H16 screen for the installer dialog.")

            technician_prompt(
                "The APK installer should now appear on the H16 screen.\n"
                "    On the H16:\n"
                "      1. If asked to choose an app to open the file, select 'Package Installer'\n"
                "         (do NOT select xbrowser or any browser)\n"
                "      2. Tap 'Install'\n"
                "      3. Wait for the 'App installed' confirmation\n"
                "    If nothing appeared on screen:\n"
                "      - Check H16 notifications (swipe down from top)\n"
                "      - Or open the Files app on H16 and tap tailscale.apk manually"
            )

        # 11. Check if Chrome is installed
        step("Checking if Chrome is installed (required for Tailscale sign-in)")
        rc, out, _ = adb_run("shell", "pm", "list", "packages")
        chrome_installed = CHROME_PKG in out
        if not chrome_installed:
            technician_prompt(
                "Chrome is NOT installed on the H16.\n"
                "    Chrome is required because the default H16 browser (xbrowser)\n"
                "    is blocked by Google authentication — Tailscale sign-in will fail in xbrowser.\n"
                "\n"
                "    On the H16:\n"
                "      1. Open the Play Store\n"
                "      2. Search for 'Google Chrome'\n"
                "      3. Install Chrome\n"
                "      4. When prompted, set Chrome as the default browser"
            )
            info("Assuming Chrome is now installed and set as default.")
        else:
            info("Chrome is installed.")

        # 12/13. Manual Tailscale sign-in
        #   Deep link auth (adb shell am start <tailscale-deep-link>) does NOT work on H16 v1.24.2.
        #   Auth key login via the CLI also does not work on this version.
        #   Manual browser sign-in through Chrome is the only supported method.
        step("Tailscale sign-in (manual — deep link and auth key login unsupported on H16 v1.24.2)")
        info("IMPORTANT: Do not attempt deep link or adb auth shortcuts — they fail silently on this device.")
        technician_prompt(
            "On the H16 device:\n"
            "      1. Open the Tailscale app\n"
            "      2. Tap 'Sign In'\n"
            "      3. If a 'Choose Browser' dialog appears, select Chrome\n"
            "         (if xbrowser opens instead, go back and set Chrome as default browser)\n"
            "      4. Sign in with your Microsoft work account in Chrome\n"
            "      5. Approve the 'Tailscale would like to set up a VPN' request on the H16\n"
            "      6. Wait until the Tailscale app shows 'Connected' with a green indicator\n"
            "\n"
            "    NOTE: If Chrome shows a Google sign-in error, make sure you are using\n"
            "    your Microsoft/work account, not a personal Google account."
        )

        # 14. Verify Tailscale is installed and running
        step("Verifying Tailscale installation")
        rc, out, _ = adb_run("shell", "dumpsys", "package", TAILSCALE_PKG, timeout=15)
        if not out.strip() or TAILSCALE_PKG not in out:
            fatal(
                "Could not confirm Tailscale is installed on the H16.\n"
                "Please verify on the H16 that the Tailscale app is visible in the app drawer."
            )
        version_match = re.search(r"versionName=([^\s\r\n]+)", out)
        ts_version = version_match.group(1) if version_match else "unknown"
        info(f"Tailscale installed on device, version: {ts_version}")
        _log(f"Tailscale version on device: {ts_version}")

    else:
        # Device was already enrolled — get WiFi IP for persistent ADB step
        step("Reading WiFi IP for persistent ADB setup")
        rc, out, _ = adb_run("shell", "ifconfig", "wlan0")
        match = re.search(r"inet addr[:\s]+(\d+\.\d+\.\d+\.\d+)", out)
        if not match:
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if match:
            wifi_ip = match.group(1)
            info(f"WiFi IP: {wifi_ip}")
        else:
            info("Could not read WiFi IP — continuing without WiFi IP info.")

    # 15. Enable persistent ADB over network
    #   Sets the ADB TCP port permanently so the device is reachable after reboot
    #   without needing USB again.
    step("Enabling persistent ADB over network (port 5555)")
    try:
        rc, out, err = adb_run("shell", "setprop", "service.adb.tcp.port", "5555", timeout=10)
        if rc != 0:
            info(f"  setprop warning (non-fatal): {err.strip()}")
        else:
            info("  ADB TCP port set to 5555.")

        time.sleep(1)
        adb_run("shell", "stop", "adbd", timeout=10)
        time.sleep(1)
        adb_run("shell", "start", "adbd", timeout=10)
        info("  ADB daemon restarted — will listen on port 5555 persistently.")
        _log("Persistent ADB over network enabled on port 5555.")
    except Exception as e:
        info(f"  Warning: Could not fully configure persistent ADB: {e}")
        info("  This can be configured manually on the device if needed.")

    # 16. Try to read Tailscale IP from device
    tailscale_ip: str | None = None
    try:
        rc, out, _ = adb_run("shell", "ip", "addr", "show", "tailscale0", timeout=10)
        match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
        if match:
            tailscale_ip = match.group(1)
    except Exception:
        pass

    # 17. Log and print result
    device_id = usb_serial or wifi_ip or "unknown"
    _log(
        f"H16 enrollment complete | device={device_id} | "
        f"wifi_ip={wifi_ip} | tailscale_ip={tailscale_ip}"
    )

    print("\n" + "=" * 60)
    print("  ENROLLMENT COMPLETE")
    print("=" * 60)
    if tailscale_ip:
        print(f"  Tailscale IP  : {tailscale_ip}")
    else:
        print("  Tailscale IP  : Check https://login.tailscale.com/admin/machines")
    if wifi_ip:
        print(f"  WiFi ADB addr : {wifi_ip}:5555 (persistent)")
    print("  Device is enrolled and ready for remote management.")
    print("=" * 60)


if __name__ == "__main__":
    main()
