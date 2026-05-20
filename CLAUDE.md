# Fleet Enrollment Package — Project Context

## What this project is

A field-deployable enrollment package that connects drone ground control devices to a
secure fleet management network. Field technicians (non-engineers) run one script per
device type; the scripts install Tailscale (mesh VPN) and RustDesk (remote desktop),
configure SSH/WinRM remote access, and report the device's Tailscale IP on completion.

## Device types

| Device | OS | Script | Run on |
|---|---|---|---|
| H16 hand controller | Android | `enroll_h16.py` | Windows technician laptop (connects via ADB) |
| GCS laptop | Windows | `enroll_gcs.ps1` | The GCS laptop itself (as Administrator) |
| Jetson companion computer | Ubuntu/Linux | `enroll_jetson.sh` | The Jetson itself (as root/sudo) |

## File layout

```
fleet-enrollment/
├── config.env              # Live config — fill in before distributing (not committed after filling)
├── config.env.example      # Template with all keys and explanatory comments
├── enroll_h16.py           # H16 enrollment (Python 3, Windows only)
├── enroll_gcs.ps1          # GCS laptop enrollment (PowerShell 5.1+, must run as Administrator)
├── enroll_jetson.sh        # Jetson enrollment (bash, must run as root/sudo)
├── README.txt              # Field technician instructions — plain language, no programming assumed
├── enrollment_log.txt      # Written at runtime next to the script (git-ignored)
├── platform-tools-r33/
│   └── adb.exe             # Bundled ADB 1.0.41 — H16 requires this exact version (v37.x writes 0 bytes)
└── apks/
    ├── tailscale-1.24.2.apk  # Bundled for H16 (adb install fails silently; push + am start required)
    └── rustdesk.apk          # Bundled for H16 — must be placed manually before running enroll_h16.py
```

## config.env keys

| Key | Used by | Notes |
|---|---|---|
| `TAILSCALE_AUTH_KEY` | All scripts | Ephemeral pre-auth key from Tailscale admin console |
| `S3_BUCKET` | Jetson | S3 bucket name for log uploads (no `s3://` prefix) |
| `EC2_TAILSCALE_IP` | Jetson, GCS (RustDesk), H16 (RustDesk) | Tailscale IP of the management EC2 server |
| `FLEET_SSH_PUBLIC_KEY` | GCS, Jetson | Full public key line added to `authorized_keys` |
| `WINRM_TRUSTED_RANGE` | GCS | Tailscale CGNAT range (`100.64.0.0/10`) — normally unchanged |
| `RUSTDESK_KEY` | GCS, Jetson, H16 | hbbs public key from `id_ed25519.pub` on EC2; optional — scripts skip RustDesk if missing |

## What each script does

### enroll_h16.py (Python)

1. Loads `config.env`
2. Verifies bundled ADB is version 1.0.41 (critical — newer ADB writes 0-byte files to H16 via USB)
3. Detects H16 via USB ADB
4. Reads WiFi IP, switches ADB to TCP/WiFi mode (all file transfers must go over WiFi — USB push is broken by H16 firmware)
5. Prompts technician to unplug USB
6. Pushes Tailscale APK over WiFi ADB with 3-attempt retry + size verification
7. Launches APK installer via `am start` (standard `adb install` fails silently on H16)
8. Checks Chrome is installed (required for Tailscale sign-in — xbrowser fails Google auth)
9. Prompts technician to sign into Tailscale manually (deep link / auth key login unsupported on H16 v1.24.2)
10. Verifies Tailscale is installed
11. Enables persistent ADB over network (port 5555) via `setprop` + adbd restart
12. Pushes RustDesk APK and installs it; attempts server auto-config via `rustdesk://server?k=KEY&r=IP` deep link
13. Reads and logs Tailscale IP from `tailscale0` interface

**Frozen binary note:** The script is designed to be compiled with PyInstaller `--onefile`. Bundled files (ADB, APKs) are read from `sys._MEIPASS`; user-editable files (`config.env`, log) live next to the exe (`sys.executable`).

### enroll_gcs.ps1 (PowerShell)

1. Requires Administrator
2. Loads `config.env`
3. Downloads and installs Tailscale if absent (from `pkgs.tailscale.com/stable/tailscale-setup-latest.exe`)
4. Authenticates Tailscale with pre-auth key
5. Verifies connection and reads Tailscale IP
6. Installs OpenSSH Server Windows capability
7. Adds fleet SSH public key to `C:\ProgramData\ssh\administrators_authorized_keys` with strict ACL
8. Updates `sshd_config` with `Match Group administrators` block; restarts sshd
9. Enables WinRM (`Enable-PSRemoting`), sets `TrustedHosts` to Tailscale range, enables Basic auth
10. Creates inbound firewall rules for SSH (22) and WinRM (5985) scoped to Tailscale range only
11. Downloads and installs RustDesk; writes `RustDesk2.toml` to `C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\RustDesk\config\`; starts and auto-enables the RustDesk service; logs the RustDesk ID

All steps are idempotent — re-running the script on an already-enrolled device is safe.

### enroll_jetson.sh (bash)

1. Requires root/sudo
2. Loads `config.env` via `source` with `allexport`
3. Checks for existing Tailscale enrollment (skips install if already connected)
4. Installs Tailscale via official install script (`curl | bash` or `wget`)
5. Authenticates with `tailscale up --authkey=... --ssh` (Tailscale SSH enabled)
6. Enables `tailscaled` service on boot
7. Installs AWS CLI v2 (architecture-aware: aarch64 or x86_64)
8. Adds fleet SSH public key to `~ubuntu/.ssh/authorized_keys` (target user detected via `$SUDO_USER`)
9. Creates `/home/ubuntu/scripts/` directory
10. Writes `/etc/fleet/config.env` (runtime config for background scripts)
11. Writes `on_landing.sh` — compresses `/home/ubuntu/logs/` and uploads to S3; called post-flight and on cron
12. Writes `ensure_tailscale.sh` — called on boot to reconnect Tailscale after 10s delay
13. Installs cron jobs: log push every 6h, Tailscale ensure on `@reboot`
14. Hardens SSH: disables password auth, restricts `ListenAddress` to Tailscale IP + loopback only
15. Downloads and installs RustDesk `.deb` (arch-aware); writes `RustDesk2.toml` for both the ubuntu user and root; enables linger for the user session; retrieves and logs RustDesk ID

All steps are idempotent.

## Remote access methods by device

| Method | GCS | Jetson | H16 |
|---|---|---|---|
| SSH (key auth only) | port 22, Tailscale range only | Tailscale interface only | — |
| WinRM | port 5985, Tailscale range only | — | — |
| ADB over network | — | — | port 5555, persistent |
| RustDesk (GUI) | installed as service | installed, needs display session | APK installed manually |

## RustDesk / GUI access

RustDesk uses a self-hosted relay on the EC2 management server (same box as `EC2_TAILSCALE_IP`).

**EC2 server setup (one-time):**
- Run `hbbs -r <EC2_TAILSCALE_IP>` and `hbbr` on the EC2 server
- Copy contents of the generated `id_ed25519.pub` into `RUSTDESK_KEY` in `config.env`
- Open ports 21115–21119 on the EC2 server (Tailscale mesh only)

**RustDesk config file path per platform:**
- Windows (service): `C:\Windows\ServiceProfiles\LocalService\AppData\Roaming\RustDesk\config\RustDesk2.toml`
- Linux: `~<user>/.config/rustdesk/RustDesk2.toml` and `/root/.config/rustdesk/RustDesk2.toml`
- Android: configured via `rustdesk://server?k=KEY&r=IP` deep link after APK install

**RustDesk version:** 1.3.9 (hardcoded in scripts — update `$rustdeskVersion` in `enroll_gcs.ps1` and `RUSTDESK_VERSION` in `enroll_jetson.sh` when upgrading).

**H16 APK:** must be manually placed as `apks/rustdesk.apk` before running `enroll_h16.py`. Download from github.com/rustdesk/rustdesk/releases.

## Key constraints and quirks

- **ADB version on H16:** Must use bundled `platform-tools-r33/adb.exe` (v1.0.41). ADB v37.x has a firmware interaction bug that causes `adb push` to write 0 bytes to H16 over USB. This is why all file transfers switch to WiFi ADB.
- **H16 APK install method:** Standard `adb install` fails with a silent EOF on H16. The workaround is `adb push` the APK to `/sdcard/` then launch it with `am start -t application/vnd.android.package-archive -d file://...`.
- **H16 Tailscale auth:** Deep link auth and CLI auth key login do not work on Tailscale v1.24.2 for Android. Manual browser sign-in via Chrome is the only supported path.
- **H16 browser:** Tailscale sign-in fails in xbrowser (default H16 browser) because Google OAuth blocks it. Chrome must be installed and set as default before signing in.
- **GCS `authorized_keys` path:** OpenSSH on Windows uses `C:\ProgramData\ssh\administrators_authorized_keys` for Administrator-group users (not `~\.ssh\authorized_keys`). The file must have inheritance disabled — SYSTEM and Administrators only.
- **Jetson SSH hardening:** `ListenAddress` is set to the Tailscale IP only, making SSH unreachable from the LAN. Tailscale must be connected before SSH will accept connections.
- **RustDesk on headless Jetson:** RustDesk requires an active display server session to share the screen. On Jetsons running headless, SSH/WinRM is the primary access method; RustDesk works only when a monitor is attached.

## Enrollment output

Every script prints on completion:
```
ENROLLMENT COMPLETE
Hostname      : <name>
Tailscale IP  : 100.x.x.x
...
RustDesk ID   : <id or "Open RustDesk app to view">
```

All steps are also written to `enrollment_log.txt` in the same folder as the script.
