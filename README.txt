FLEET ENROLLMENT PACKAGE
========================
For field technicians — no programming knowledge required.


WHAT THIS PACKAGE DOES
-----------------------
This package connects a drone ground control device to the fleet management
network so that the engineering team can remotely access and maintain it from
headquarters. After running the script on a device, it joins a secure private
network (Tailscale) and the engineering team can reach it without you needing
to do anything further.


BEFORE YOU START — READ THIS FIRST
------------------------------------
Before enrolling any device, confirm ALL of the following:

  [ ] You have a copy of this fleet-enrollment folder (from a USB drive or
      shared location). Do not run scripts from partial copies.

  [ ] The config.env file inside this folder has been filled in with real
      values (it should NOT contain "xxxxxx" or "AAAA...").
      If it still has placeholder values, contact the engineering team.

  [ ] If enrolling an H16 and GUI access is required, confirm that
      rustdesk.apk is present in the apks/ folder alongside tailscale.apk.

  [ ] The device you are enrolling is fully powered on and functional.

  [ ] You have a WiFi network available that the device can connect to
      (required for H16 hand controllers).

  [ ] For H16 controllers: You have a Windows laptop and a USB-A cable.

  [ ] For GCS laptops: You know the Administrator password for that laptop.

  [ ] For Jetson computers: You have physical access or a connected display
      and keyboard.

If any of the above is not ready, sort that out before proceeding.


===================================================================
ENROLLING AN H16 HAND CONTROLLER
===================================================================

What you need:
  - A Windows laptop (you will run the script on this laptop)
  - A USB cable connecting the H16 to the laptop
  - The H16 must be connected to a WiFi network
  - Python 3 installed on the Windows laptop

STEP 1 — Prepare the H16 (do this before plugging in USB)
  a. On the H16, go to: Settings > About Phone
  b. Tap "Build Number" seven times in a row
     (you will see a message: "You are now a developer")
  c. Go back to: Settings > Developer Options
  d. Turn ON "USB Debugging"
  e. Turn ON "ADB over Network"
  f. Go to: Settings > Security
  g. Turn ON "Unknown Sources" (allows installing apps from files)

STEP 2 — Connect the H16 to your laptop
  a. Plug the USB cable from the H16 into your Windows laptop
  b. On the H16 screen, a dialog will appear: "Allow USB Debugging?"
  c. Tap "Always allow from this computer", then tap "OK"
  d. Confirm the H16 is connected to a WiFi network (check the top bar)

STEP 3 — Run the enrollment script
  a. Open a Command Prompt or PowerShell window on the Windows laptop
     (press Windows key, type "cmd", press Enter)
  b. Type the following and press Enter:
       cd C:\path\to\fleet-enrollment
     (replace the path with wherever this folder is saved)
  c. Run:
       python enroll_h16.py
  d. Follow all on-screen instructions carefully — read each message

STEP 4 — What happens during the script
  The script will guide you step by step. You will be asked to:

  a. UNPLUG the USB cable when the script tells you to.
     Do not unplug it before you are told — wait for the prompt.

  b. Install the Tailscale app when the installer appears on the H16 screen:
       - If asked which app to use, choose "Package Installer"
         (do not choose a browser)
       - Tap "Install"
       - Wait for "App installed"

  c. If Chrome is not installed, install it from the Play Store:
       - Open Play Store on H16
       - Search for "Google Chrome"
       - Install it, then set it as the default browser

  d. Sign into Tailscale on the H16:
       - Open the Tailscale app
       - Tap "Sign In"
       - If asked which browser to use, choose Chrome
         (IMPORTANT: do NOT use xbrowser — it will fail)
       - Sign in with your Microsoft work account
       - Approve the VPN connection request
       - Wait until Tailscale shows "Connected"

STEP 5 — Enrollment complete
  When finished, the script will print:

    ENROLLMENT COMPLETE
    Tailscale IP: 100.x.x.x

  Write down the Tailscale IP address and report it to the engineering team.


===================================================================
ENROLLING A GCS WINDOWS LAPTOP
===================================================================

What you need:
  - The GCS Windows laptop itself (you run the script on the laptop directly)
  - Administrator login credentials for that laptop

STEP 1 — Copy the enrollment package to the GCS laptop
  - If using a USB drive: plug it in, open it in File Explorer,
    and copy the entire fleet-enrollment folder to the Desktop
  - If the package is on a network share: copy it to the Desktop

STEP 2 — Run the enrollment script as Administrator
  Option A (easiest):
    - Right-click on "enroll_gcs.ps1"
    - Select "Run with PowerShell"
    - Click "Yes" if Windows asks for permission

  Option B (if Option A doesn't work):
    - Click Start, type "PowerShell"
    - Right-click "Windows PowerShell" and choose "Run as Administrator"
    - In the PowerShell window, type:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
        cd $HOME\Desktop\fleet-enrollment
        .\enroll_gcs.ps1
    - Press Enter after each line

STEP 3 — The script runs automatically
  It will install Tailscale, configure SSH and WinRM remote access,
  and set up firewall rules. This takes about 2-5 minutes.
  No interaction is required — just wait.

STEP 4 — Enrollment complete
  When finished, the script will print:

    ENROLLMENT COMPLETE
    Tailscale IP: 100.x.x.x

  Write down the Tailscale IP address and report it to the engineering team.


===================================================================
ENROLLING A JETSON COMPANION COMPUTER
===================================================================

What you need:
  - Physical access to the Jetson (connected monitor and keyboard),
    OR SSH access from another machine on the same network
  - The Jetson must have internet access

STEP 1 — Copy the enrollment package to the Jetson
  From USB drive:
    - Plug the USB drive into the Jetson
    - Copy the fleet-enrollment folder to /home/ubuntu/

  From another computer over the network:
    scp -r fleet-enrollment/ ubuntu@<jetson-ip>:~/

STEP 2 — Open a terminal on the Jetson

STEP 3 — Run the enrollment script
  In the terminal, run:
    cd ~/fleet-enrollment
    sudo bash enroll_jetson.sh

  Enter the Jetson password when prompted (for sudo).

STEP 4 — The script runs automatically
  It will install Tailscale, configure SSH key access, install the
  AWS log upload tool, and set up scheduled tasks. This takes
  about 3-8 minutes depending on internet speed.

STEP 5 — Enrollment complete
  When finished, the script will print:

    ENROLLMENT COMPLETE
    Tailscale IP: 100.x.x.x

  Write down the Tailscale IP address and report it to the engineering team.


===================================================================
GUI ACCESS (RUSTDESK)
===================================================================

After enrollment, the engineering team can get a full graphical desktop
view of GCS laptops, Jetson computers, and H16 hand controllers using
RustDesk — an open-source remote desktop tool.

PREREQUISITES (engineering team — one-time EC2 server setup)
-------------------------------------------------------------
Before enrolling any device with GUI access, the EC2 management server
must be running the RustDesk relay components:

  1. On the EC2 server, download and run hbbs (ID server) and hbbr (relay):
       ./hbbs -r <EC2_TAILSCALE_IP>
       ./hbbr

  2. After hbbs starts for the first time it creates id_ed25519.pub in
     the same directory. Copy the entire contents of that file and paste
     it as the RUSTDESK_KEY value in config.env before enrolling devices.

     Also set RUSTDESK_PRESET_PASSWORD in config.env to a strong shared
     password. This is the permanent password used for unattended access,
     so the team can connect without anyone present at the device. If it is
     left blank, devices fall back to a one-time password shown on screen.

  3. Ports required on the EC2 server (Tailscale mesh only):
       21115 TCP — hbbs (NAT test)
       21116 TCP/UDP — hbbs (peer ID registration)
       21117 TCP — hbbr (relay traffic)
       21118 TCP — hbbs (websocket)
       21119 TCP — hbbr (websocket)

CONNECTING TO A DEVICE
----------------------
  1. Install RustDesk on your engineering workstation:
       https://rustdesk.com (download the desktop client)

  2. In RustDesk settings, set the relay server:
       Network > ID/Relay Server > enter EC2_TAILSCALE_IP
       Key > paste RUSTDESK_KEY

  3. Enter the device's RustDesk ID (printed at the end of enrollment
     or visible in the RustDesk app on the device) and click Connect.

  4. When prompted for a password, use the permanent password that was set
     as RUSTDESK_PRESET_PASSWORD during enrollment. (If that was left blank,
     use the one-time password shown in the RustDesk app on the device.)

H16 HAND CONTROLLER NOTES
  - After enrollment, open the RustDesk app on the H16 to see its ID.
  - The H16 must have the RustDesk APK placed in the apks/ folder
    before running enroll_h16.py. Download rustdesk.apk from
    https://github.com/rustdesk/rustdesk/releases and rename it
    to rustdesk.apk in the apks/ folder.
  - If RUSTDESK_PRESET_PASSWORD is set in config.env, the script pauses and
    asks the technician to set that same password in the RustDesk app
    (Settings > Security > permanent password). Android has no command-line
    way to set it automatically, so this step is manual.

JETSON NOTES
  - RustDesk on the Jetson requires an active display session to share
    the screen. If the Jetson runs headless, SSH remains the primary
    remote access method; RustDesk will work when a monitor is connected.

===================================================================
HOW TO CONFIRM ENROLLMENT WORKED
===================================================================

After running any enrollment script:

  1. The script ended with "ENROLLMENT COMPLETE" — not an error message
  2. A Tailscale IP address starting with "100." was displayed
  3. Enrollment was logged to: enrollment_log.txt (in the same folder)

Per device type:
  - H16: The Tailscale app shows "Connected" with a green indicator
  - GCS laptop: The Tailscale icon in the system tray (bottom-right) is lit
  - Jetson: Running "tailscale status" shows the device as connected

Report the Tailscale IP to the engineering team. They will verify the device
appears in the fleet management dashboard.


===================================================================
TROUBLESHOOTING — WHAT TO DO WHEN SOMETHING GOES WRONG
===================================================================

The scripts always print a clear error message explaining what went wrong
and what to check. Read the full error message before doing anything else.

Common issues by device type:

--- H16 ---

"No H16 detected via USB ADB"
  -> Enable USB Debugging in H16 Developer Options
  -> Unplug and re-plug the USB cable
  -> Accept the "Allow USB Debugging" popup on the H16 screen

"Device is in state offline"
  -> Unplug and re-plug USB, accept the ADB prompt on H16
  -> Try a different USB cable or USB port

"APK arrived as 0 bytes after 3 attempts"
  -> The script must use WiFi ADB, not USB
  -> Make sure "ADB over Network" is enabled in H16 Developer Options
  -> Make sure the H16 is connected to WiFi before starting the script

Tailscale sign-in opens xbrowser and fails
  -> Go back and install Google Chrome from the Play Store
  -> Set Chrome as the default browser when asked
  -> Try signing into Tailscale again

--- GCS Laptop ---

"Must be run as Administrator"
  -> Right-click the script, choose "Run as Administrator"
  -> Or open an Administrator PowerShell window

"tailscale up failed"
  -> The auth key in config.env may be expired
  -> Contact engineering for a fresh auth key
  -> Update config.env and run the script again

Script won't run / "execution of scripts is disabled"
  -> Open Administrator PowerShell and run:
       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
     Then run the script again in that same window

--- Jetson ---

"Neither curl nor wget is installed"
  -> Run: sudo apt-get install curl
  -> Then re-run the enrollment script

"tailscale up failed"
  -> The auth key in config.env may be expired
  -> Contact engineering for a fresh key and update config.env

"Permission denied" when running the script
  -> Make sure you are using: sudo bash enroll_jetson.sh
  -> Do not run it as: bash enroll_jetson.sh (without sudo)

---

If you cannot resolve the problem yourself, contact the engineering team
and include:
  - Which device type (H16, GCS laptop, or Jetson)
  - The exact error message shown on screen
  - The enrollment_log.txt file from the fleet-enrollment folder

Engineering Support Email : [CONTACT EMAIL PLACEHOLDER]
Engineering Phone / Slack  : [CONTACT PLACEHOLDER]
