#!/usr/bin/env bash
# Jetson Companion Computer Fleet Enrollment Script
# Run on the Jetson itself as root or with sudo.
# Usage: sudo bash enroll_jetson.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/enrollment_log.txt"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_log() {
    local ts
    ts="$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S')"
    printf "[%s] %s\n" "$ts" "$*" >> "$LOG_FILE"
}

fatal() {
    printf "\n[ERROR] %s\n" "$*" >&2
    _log "ERROR: $*"
    exit 1
}

step() {
    printf "\n>>> %s\n" "$*"
    _log "STEP: $*"
}

info() {
    printf "    %s\n" "$*"
}

# ---------------------------------------------------------------------------
# Step 1: Check root / sudo
# ---------------------------------------------------------------------------

step "Checking for root/sudo privileges"
if [[ $EUID -ne 0 ]]; then
    fatal "This script must be run as root or with sudo.\nUsage: sudo bash enroll_jetson.sh"
fi
info "Running as root."

# ---------------------------------------------------------------------------
# Step 2: Load configuration from config.env
# ---------------------------------------------------------------------------

step "Loading configuration from config.env"
CONFIG_FILE="$SCRIPT_DIR/config.env"
[[ -f "$CONFIG_FILE" ]] || fatal "config.env not found at: $CONFIG_FILE"

# Export all KEY=VALUE pairs into the environment (skip comments and blank lines)
set -o allexport
# shellcheck source=/dev/null
source "$CONFIG_FILE"
set +o allexport

# Validate required keys
: "${TAILSCALE_AUTH_KEY:?TAILSCALE_AUTH_KEY not set in config.env}"
: "${S3_BUCKET:?S3_BUCKET not set in config.env}"
: "${FLEET_SSH_PUBLIC_KEY:?FLEET_SSH_PUBLIC_KEY not set in config.env}"
: "${EC2_TAILSCALE_IP:?EC2_TAILSCALE_IP not set in config.env}"

if [[ "$TAILSCALE_AUTH_KEY" == *"xxxxxx"* ]]; then
    fatal "TAILSCALE_AUTH_KEY is still a placeholder. Set a real key in config.env."
fi
info "Config loaded."

# ---------------------------------------------------------------------------
# Determine target user (the non-root user who invoked sudo, or 'ubuntu')
# ---------------------------------------------------------------------------
TARGET_USER="${SUDO_USER:-ubuntu}"
if ! id "$TARGET_USER" &>/dev/null; then
    TARGET_USER="root"
fi
TARGET_HOME="$(eval echo ~"$TARGET_USER")"
HOSTNAME_ID="$(hostname)"

# ---------------------------------------------------------------------------
# Check if already enrolled (idempotency guard)
# ---------------------------------------------------------------------------

step "Checking if device is already enrolled"
ALREADY_ENROLLED=false
if command -v tailscale &>/dev/null; then
    TS_IP="$(tailscale ip -4 2>/dev/null || true)"
    if [[ -n "$TS_IP" ]]; then
        info "Tailscale already connected: $TS_IP"
        info "Skipping Tailscale install and authentication steps."
        ALREADY_ENROLLED=true
    fi
fi

# ---------------------------------------------------------------------------
# Step 3: Install Tailscale
# ---------------------------------------------------------------------------

if [[ "$ALREADY_ENROLLED" != "true" ]]; then
    step "Installing Tailscale via official install script"
    if command -v tailscale &>/dev/null; then
        info "Tailscale binary already present."
    else
        info "Downloading and running Tailscale install script..."
        if command -v curl &>/dev/null; then
            curl -fsSL https://tailscale.com/install.sh | bash
        elif command -v wget &>/dev/null; then
            wget -qO- https://tailscale.com/install.sh | bash
        else
            fatal "Neither curl nor wget is installed.\nInstall one with: apt-get install curl\nThen re-run this script."
        fi
        info "Tailscale installed."
        _log "Tailscale installed."
    fi

    # Step 4: Authenticate
    step "Authenticating Tailscale and enabling Tailscale SSH"
    info "Running: tailscale up --authkey=<key> --ssh"
    if ! tailscale up --authkey="$TAILSCALE_AUTH_KEY" --ssh; then
        fatal (
            "tailscale up failed.\n" \
            "Check that TAILSCALE_AUTH_KEY in config.env is valid and not expired.\n" \
            "Generate a new key at: https://login.tailscale.com/admin/settings/keys"
        )
    fi
    info "Tailscale authenticated."
    _log "Tailscale authenticated."
fi

# Step 5: Enable tailscaled on boot
step "Enabling tailscaled service on boot"
if systemctl enable tailscaled 2>/dev/null; then
    info "tailscaled enabled for autostart."
    _log "tailscaled enabled on boot."
else
    info "Warning: Could not enable tailscaled via systemctl — may already be enabled."
fi

# Read Tailscale IP (required for SSH hardening later)
TS_IP="$(tailscale ip -4 2>/dev/null || true)"
if [[ -z "$TS_IP" ]]; then
    fatal "Tailscale is not connected. Check 'tailscale status' and re-run."
fi
info "Tailscale IP: $TS_IP"
_log "Tailscale IP: $TS_IP"

# ---------------------------------------------------------------------------
# Step 6: Install AWS CLI
# ---------------------------------------------------------------------------

step "Installing AWS CLI"
if command -v aws &>/dev/null; then
    info "AWS CLI already installed: $(aws --version 2>&1 | head -1)"
    _log "AWS CLI already present."
else
    info "Installing AWS CLI v2 ..."

    # Install prerequisites
    if command -v apt-get &>/dev/null; then
        apt-get update -qq
        apt-get install -y -qq unzip curl 2>/dev/null || true
    fi

    ARCH="$(uname -m)"
    if [[ "$ARCH" == "aarch64" ]]; then
        AWS_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
    else
        AWS_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
    fi

    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT

    info "Downloading AWS CLI from $AWS_URL ..."
    curl -fsSL "$AWS_URL" -o "$TMP_DIR/awscliv2.zip"
    unzip -q "$TMP_DIR/awscliv2.zip" -d "$TMP_DIR"

    "$TMP_DIR/aws/install" --update
    trap - EXIT
    rm -rf "$TMP_DIR"

    if ! command -v aws &>/dev/null; then
        fatal "AWS CLI installation failed. Check logs above for errors."
    fi
    info "AWS CLI installed: $(aws --version 2>&1 | head -1)"
    _log "AWS CLI installed."
fi

# ---------------------------------------------------------------------------
# Step 7: Add fleet SSH public key to authorized_keys
# ---------------------------------------------------------------------------

step "Adding fleet SSH public key to authorized_keys"
SSH_DIR="$TARGET_HOME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
chown "$TARGET_USER:$TARGET_USER" "$SSH_DIR"

if [[ -f "$AUTH_KEYS" ]] && grep -qF "$FLEET_SSH_PUBLIC_KEY" "$AUTH_KEYS" 2>/dev/null; then
    info "Fleet SSH key already present in authorized_keys."
else
    printf "%s\n" "$FLEET_SSH_PUBLIC_KEY" >> "$AUTH_KEYS"
    info "Fleet SSH key added to: $AUTH_KEYS"
    _log "Fleet SSH public key added to authorized_keys for $TARGET_USER."
fi

chmod 600 "$AUTH_KEYS"
chown "$TARGET_USER:$TARGET_USER" "$AUTH_KEYS"

# ---------------------------------------------------------------------------
# Step 8: Create scripts directory
# ---------------------------------------------------------------------------

step "Creating scripts directory at /home/ubuntu/scripts"
SCRIPTS_DIR="/home/ubuntu/scripts"
mkdir -p "$SCRIPTS_DIR"
if id "ubuntu" &>/dev/null; then
    chown -R ubuntu:ubuntu "$SCRIPTS_DIR" 2>/dev/null || true
fi
info "Scripts directory ready: $SCRIPTS_DIR"

# Save config to a permanent system location so on_landing.sh can read it
FLEET_CONFIG_DIR="/etc/fleet"
FLEET_CONFIG_FILE="$FLEET_CONFIG_DIR/config.env"
mkdir -p "$FLEET_CONFIG_DIR"
chmod 700 "$FLEET_CONFIG_DIR"
cat > "$FLEET_CONFIG_FILE" << EOF
# Fleet config — written by enroll_jetson.sh
S3_BUCKET=$S3_BUCKET
EC2_TAILSCALE_IP=$EC2_TAILSCALE_IP
EOF
chmod 600 "$FLEET_CONFIG_FILE"
info "Fleet config saved to $FLEET_CONFIG_FILE"

# ---------------------------------------------------------------------------
# Step 9: Write on_landing.sh (log compression and S3 upload)
# ---------------------------------------------------------------------------

step "Writing on_landing.sh"
cat > "$SCRIPTS_DIR/on_landing.sh" << 'LANDING_EOF'
#!/usr/bin/env bash
# Compresses logs from /home/ubuntu/logs/ and uploads to S3.
# Called every 6 hours via cron and on demand after a drone landing.
set -euo pipefail

FLEET_CONFIG="/etc/fleet/config.env"
if [[ -f "$FLEET_CONFIG" ]]; then
    set -o allexport
    # shellcheck source=/dev/null
    source "$FLEET_CONFIG"
    set +o allexport
fi

S3_BUCKET="${S3_BUCKET:-}"
LOG_DIR="${LOG_DIR:-/home/ubuntu/logs}"
HOSTNAME_ID="$(hostname)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

if [[ -z "$S3_BUCKET" ]]; then
    echo "[on_landing] ERROR: S3_BUCKET not configured in $FLEET_CONFIG" >&2
    exit 1
fi

if [[ ! -d "$LOG_DIR" ]]; then
    echo "[on_landing] No log directory at $LOG_DIR — nothing to upload."
    exit 0
fi

# Check directory is non-empty
if [[ -z "$(ls -A "$LOG_DIR" 2>/dev/null)" ]]; then
    echo "[on_landing] Log directory is empty — nothing to upload."
    exit 0
fi

ARCHIVE="/tmp/logs_${HOSTNAME_ID}_${TIMESTAMP}.tar.gz"
echo "[on_landing] Compressing logs from $LOG_DIR ..."
if ! tar -czf "$ARCHIVE" -C "$LOG_DIR" . 2>/dev/null; then
    echo "[on_landing] Warning: Could not compress logs — archive may be incomplete."
fi

S3_KEY="logs/${HOSTNAME_ID}/${TIMESTAMP}.tar.gz"
echo "[on_landing] Uploading to s3://${S3_BUCKET}/${S3_KEY} ..."
if aws s3 cp "$ARCHIVE" "s3://${S3_BUCKET}/${S3_KEY}"; then
    echo "[on_landing] Upload complete: s3://${S3_BUCKET}/${S3_KEY}"
else
    echo "[on_landing] ERROR: S3 upload failed. Check AWS credentials and bucket permissions." >&2
    rm -f "$ARCHIVE"
    exit 1
fi

rm -f "$ARCHIVE"
LANDING_EOF

chmod +x "$SCRIPTS_DIR/on_landing.sh"
info "on_landing.sh written to $SCRIPTS_DIR/on_landing.sh"
_log "on_landing.sh created."

# Write ensure_tailscale.sh for the @reboot cron — avoids storing auth key in crontab
cat > "$SCRIPTS_DIR/ensure_tailscale.sh" << 'TSUP_EOF'
#!/usr/bin/env bash
# Called on boot to ensure Tailscale is connected.
sleep 10   # Allow network interfaces to initialize
tailscale up --ssh 2>/dev/null || true
TSUP_EOF

chmod +x "$SCRIPTS_DIR/ensure_tailscale.sh"
info "ensure_tailscale.sh written to $SCRIPTS_DIR/ensure_tailscale.sh"

# ---------------------------------------------------------------------------
# Step 10: Set up cron jobs
# ---------------------------------------------------------------------------

step "Setting up cron jobs"

CRON_LOG_PUSH="0 */6 * * * $SCRIPTS_DIR/on_landing.sh >> /var/log/on_landing.log 2>&1"
CRON_TS_BOOT="@reboot $SCRIPTS_DIR/ensure_tailscale.sh >> /var/log/tailscale_boot.log 2>&1"

EXISTING_CRON="$(crontab -l 2>/dev/null || true)"
NEW_CRON="$EXISTING_CRON"

if echo "$EXISTING_CRON" | grep -qF "on_landing.sh"; then
    info "Log push cron already present — skipping."
else
    NEW_CRON="${NEW_CRON}${NEW_CRON:+$'\n'}${CRON_LOG_PUSH}"
    info "Added: log push every 6 hours -> S3."
fi

if echo "$EXISTING_CRON" | grep -qF "ensure_tailscale.sh"; then
    info "Tailscale boot cron already present — skipping."
else
    NEW_CRON="${NEW_CRON}${NEW_CRON:+$'\n'}${CRON_TS_BOOT}"
    info "Added: Tailscale ensure on boot."
fi

echo "$NEW_CRON" | crontab -
_log "Cron jobs configured."

# ---------------------------------------------------------------------------
# Step 11: Harden SSH
# ---------------------------------------------------------------------------

step "Hardening SSH configuration"
SSHD_CONFIG="/etc/ssh/sshd_config"

if [[ ! -f "$SSHD_CONFIG" ]]; then
    info "Warning: $SSHD_CONFIG not found — skipping SSH hardening."
else
    # Disable password authentication
    if grep -q "^PasswordAuthentication" "$SSHD_CONFIG"; then
        sed -i "s/^PasswordAuthentication.*/PasswordAuthentication no/" "$SSHD_CONFIG"
    elif grep -q "^#PasswordAuthentication" "$SSHD_CONFIG"; then
        sed -i "s/^#PasswordAuthentication.*/PasswordAuthentication no/" "$SSHD_CONFIG"
    else
        echo "PasswordAuthentication no" >> "$SSHD_CONFIG"
    fi
    info "PasswordAuthentication set to no."
    _log "SSH: PasswordAuthentication=no"

    # Restrict SSH to listen only on the Tailscale interface (and loopback)
    # This means SSH is unreachable from the LAN — only via Tailscale mesh.
    if [[ -n "$TS_IP" ]]; then
        # Remove any existing ListenAddress lines to avoid conflicts
        sed -i "/^ListenAddress/d" "$SSHD_CONFIG"
        printf "ListenAddress %s\n" "$TS_IP" >> "$SSHD_CONFIG"
        printf "ListenAddress 127.0.0.1\n" >> "$SSHD_CONFIG"
        info "SSH restricted to Tailscale interface ($TS_IP) and loopback."
        _log "SSH ListenAddress: $TS_IP and 127.0.0.1"
    else
        info "Warning: Could not determine Tailscale IP — SSH not restricted to Tailscale interface."
    fi

    # Restart SSH to apply changes
    if systemctl restart sshd 2>/dev/null || systemctl restart ssh 2>/dev/null; then
        info "SSH service restarted."
    else
        info "Warning: Could not restart SSH. Changes will take effect on next reboot."
    fi
fi

# ---------------------------------------------------------------------------
# Step 12/13: Done
# ---------------------------------------------------------------------------

_log "Jetson enrollment complete | hostname=$HOSTNAME_ID | tailscale_ip=$TS_IP"

printf "\n"
printf "============================================================\n"
printf "  ENROLLMENT COMPLETE\n"
printf "============================================================\n"
printf "  Hostname      : %s\n" "$HOSTNAME_ID"
printf "  Tailscale IP  : %s\n" "$TS_IP"
printf "  SSH           : Key auth only, Tailscale interface only\n"
printf "  Log uploads   : Every 6h via cron -> s3://%s\n" "$S3_BUCKET"
printf "  Log file      : %s\n" "$LOG_FILE"
printf "  Device is enrolled and ready for remote Ansible management.\n"
printf "============================================================\n"
