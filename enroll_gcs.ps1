<#
.SYNOPSIS
    GCS Windows Laptop Fleet Enrollment Script
    Run as Administrator on the GCS laptop itself.
.DESCRIPTION
    Installs Tailscale, OpenSSH, and WinRM. Configures firewall rules
    restricted to the Tailscale mesh range. Reads all settings from config.env.
#>

#Requires -Version 5.1

Set-StrictMode -Version Latest
# Use Continue so we can issue our own clear error messages rather than raw exceptions
$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogFile   = Join-Path $ScriptDir "enrollment_log.txt"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
    "[$ts] $Message" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host ">>> $Message" -ForegroundColor Cyan
    Write-Log "STEP: $Message"
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message"
}

function Write-Fatal {
    param([string]$Message)
    Write-Host ""
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Write-Log "ERROR: $Message"
    exit 1
}

function Find-TailscaleExe {
    foreach ($candidate in @(
        "C:\Program Files\Tailscale\tailscale.exe",
        "C:\Program Files (x86)\Tailscale\tailscale.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    try { return (Get-Command "tailscale" -ErrorAction Stop).Source } catch {}
    return $null
}

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

function Get-Config {
    $configPath = Join-Path $ScriptDir "config.env"
    if (-not (Test-Path $configPath)) {
        Write-Fatal "config.env not found at: $configPath`nRun this script from the fleet-enrollment folder."
    }
    $cfg = @{}
    foreach ($line in (Get-Content $configPath -Encoding utf8)) {
        $line = $line.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { continue }
        if ($line -match "^([^=]+)=(.*)$") {
            $cfg[$Matches[1].Trim()] = $Matches[2].Trim()
        }
    }
    return $cfg
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Write-Host "============================================================" -ForegroundColor Green
Write-Host "  GCS Windows Laptop Fleet Enrollment" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Log "=== GCS Enrollment Started ==="

# ---------------------------------------------------------------------------
# Step 1: Check Administrator privileges
# ---------------------------------------------------------------------------

Write-Step "Checking for Administrator privileges"
$currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Fatal (
        "This script must be run as Administrator.`n" +
        "Right-click 'enroll_gcs.ps1' and choose 'Run as Administrator',`n" +
        "or open an Administrator PowerShell and run:  .\enroll_gcs.ps1"
    )
}
Write-Info "Running as Administrator."

# ---------------------------------------------------------------------------
# Step 2: Load config
# ---------------------------------------------------------------------------

Write-Step "Loading configuration from config.env"
$cfg = Get-Config
$authKey       = $cfg["TAILSCALE_AUTH_KEY"]
$sshPublicKey  = $cfg["FLEET_SSH_PUBLIC_KEY"]
$winrmRange    = $cfg["WINRM_TRUSTED_RANGE"]

if (-not $authKey -or $authKey -match "xxxxxx") {
    Write-Fatal (
        "TAILSCALE_AUTH_KEY is not configured in config.env.`n" +
        "Set a valid Tailscale pre-auth key and run again."
    )
}
if (-not $sshPublicKey -or $sshPublicKey -match "AAAA\.\.\.") {
    Write-Fatal (
        "FLEET_SSH_PUBLIC_KEY is not configured in config.env.`n" +
        "Paste the full fleet management public key and run again."
    )
}
Write-Info "Config loaded."

# ---------------------------------------------------------------------------
# Step 3/4: Install Tailscale if not already present
# ---------------------------------------------------------------------------

Write-Step "Checking Tailscale installation"
$tailscaleInstalled = [bool](Get-Service -Name "Tailscale" -ErrorAction SilentlyContinue)

if ($tailscaleInstalled) {
    Write-Info "Tailscale already installed — skipping download and install."
    Write-Log "Tailscale already installed."
} else {
    Write-Step "Downloading Tailscale installer from pkgs.tailscale.com"

    # Ensure TLS 1.2 for older Windows configurations
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $installerUrl  = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
    $installerPath = Join-Path $env:TEMP "tailscale-setup.exe"

    Write-Info "Downloading: $installerUrl"
    try {
        Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing -ErrorAction Stop
    } catch {
        Write-Fatal (
            "Failed to download Tailscale installer: $_`n" +
            "Check that this laptop has internet access and try again."
        )
    }
    Write-Info "Download complete."

    Write-Step "Installing Tailscale silently"
    try {
        $proc = Start-Process -FilePath $installerPath -ArgumentList "/S" -Wait -PassThru -ErrorAction Stop
        if ($proc.ExitCode -ne 0) {
            Write-Fatal "Tailscale installer exited with code $($proc.ExitCode). Try running the installer manually."
        }
    } catch {
        Write-Fatal "Tailscale installation failed: $_"
    }
    Write-Info "Tailscale installed."

    # Wait for service to reach Running state
    Write-Info "Waiting for Tailscale service to start..."
    $deadline = (Get-Date).AddSeconds(45)
    $svc = $null
    while ((Get-Date) -lt $deadline) {
        $svc = Get-Service -Name "Tailscale" -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq "Running") { break }
        Start-Sleep -Seconds 2
    }
    if (-not $svc -or $svc.Status -ne "Running") {
        Write-Fatal (
            "Tailscale service did not start within 45 seconds.`n" +
            "Open Services (services.msc) and check the Tailscale service status."
        )
    }
    Write-Info "Tailscale service is running."
}

$tailscaleExe = Find-TailscaleExe
if (-not $tailscaleExe) {
    Write-Fatal "Cannot find tailscale.exe after installation. Please check the installation and try again."
}

# ---------------------------------------------------------------------------
# Step 5: Authenticate Tailscale
# ---------------------------------------------------------------------------

Write-Step "Authenticating Tailscale to fleet network"

# Check if already connected
$alreadyConnected = $false
try {
    $statusJson = & "$tailscaleExe" status --json 2>$null
    $statusObj  = $statusJson | ConvertFrom-Json -ErrorAction Stop
    if ($statusObj.BackendState -eq "Running" -or $statusObj.BackendState -eq "Connected") {
        $alreadyConnected = $true
    }
} catch {
    $alreadyConnected = $false
}

if ($alreadyConnected) {
    Write-Info "Tailscale already authenticated and connected."
    Write-Log "Tailscale already connected — skipping auth."
} else {
    Write-Info "Running: tailscale up --authkey=<key>"
    & "$tailscaleExe" up "--authkey=$authKey" 2>&1 | Write-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Fatal (
            "tailscale up failed (exit code $LASTEXITCODE).`n" +
            "Check that the TAILSCALE_AUTH_KEY in config.env is valid and not expired.`n" +
            "Generate a new key at: https://login.tailscale.com/admin/settings/keys"
        )
    }
    Write-Info "Tailscale authenticated."
    Write-Log "Tailscale authenticated with auth key."
}

# ---------------------------------------------------------------------------
# Step 6: Verify connection and get Tailscale IP
# ---------------------------------------------------------------------------

Write-Step "Verifying Tailscale connection"
Start-Sleep -Seconds 3

$tailscaleIp = $null
try {
    $statusJson = & "$tailscaleExe" status --json 2>$null
    $statusObj  = $statusJson | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($statusObj -and $statusObj.TailscaleIPs) {
        $tailscaleIp = $statusObj.TailscaleIPs | Where-Object { $_ -match "^\d+\.\d+\.\d+\.\d+$" } | Select-Object -First 1
    }
} catch {}

if (-not $tailscaleIp) {
    # Fallback: parse output of 'tailscale ip'
    try {
        $ipOutput    = & "$tailscaleExe" ip 2>&1
        $ipMatch     = [regex]::Match($ipOutput, "\d+\.\d+\.\d+\.\d+")
        if ($ipMatch.Success) { $tailscaleIp = $ipMatch.Value }
    } catch {}
}

if (-not $tailscaleIp) {
    Write-Fatal (
        "Tailscale does not appear to be connected.`n" +
        "Check the Tailscale tray icon or run 'tailscale status' in a terminal.`n" +
        "If the auth key expired, update config.env with a new key and run again."
    )
}
Write-Info "Tailscale IP: $tailscaleIp"
Write-Log "Tailscale IP: $tailscaleIp"

# ---------------------------------------------------------------------------
# Step 7: Install OpenSSH Server
# ---------------------------------------------------------------------------

Write-Step "Installing OpenSSH Server"
$sshdCap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue
if ($sshdCap -and $sshdCap.State -eq "Installed") {
    Write-Info "OpenSSH Server already installed."
} else {
    Write-Info "Installing OpenSSH.Server~~~~0.0.1.0 ..."
    try {
        Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0" -ErrorAction Stop | Out-Null
        Write-Info "OpenSSH Server installed."
        Write-Log "OpenSSH Server installed."
    } catch {
        Write-Fatal (
            "Failed to install OpenSSH Server: $_`n" +
            "Ensure the laptop has internet access (for Windows Update) and try again."
        )
    }
}

try {
    Start-Service sshd -ErrorAction Stop
    Write-Info "sshd service started."
} catch {
    Write-Fatal "Could not start sshd service: $_`nTry starting it manually in services.msc."
}

try {
    Set-Service -Name sshd -StartupType Automatic -ErrorAction Stop
    Write-Info "sshd configured to start automatically on boot."
} catch {
    Write-Fatal "Could not set sshd startup type: $_"
}

# ---------------------------------------------------------------------------
# Step 8: Add fleet SSH public key to authorized_keys
# ---------------------------------------------------------------------------

Write-Step "Adding fleet SSH public key to authorized_keys"

# For Administrator-group users, OpenSSH uses administrators_authorized_keys
$sshdDataDir       = "C:\ProgramData\ssh"
$authorizedKeysPath = Join-Path $sshdDataDir "administrators_authorized_keys"

if (-not (Test-Path $sshdDataDir)) {
    New-Item -ItemType Directory -Path $sshdDataDir -Force | Out-Null
}

$existingContent = ""
if (Test-Path $authorizedKeysPath) {
    $existingContent = Get-Content $authorizedKeysPath -Raw -Encoding utf8 -ErrorAction SilentlyContinue
}

if ($existingContent -and $existingContent.Contains($sshPublicKey.Trim())) {
    Write-Info "Fleet SSH key already present in authorized_keys."
} else {
    try {
        Add-Content -Path $authorizedKeysPath -Value $sshPublicKey -Encoding utf8 -ErrorAction Stop
        Write-Info "Fleet SSH key added to: $authorizedKeysPath"
        Write-Log "Fleet SSH public key added to authorized_keys."
    } catch {
        Write-Fatal "Could not write to authorized_keys: $_"
    }
}

# Fix permissions — OpenSSH rejects authorized_keys with inherited permissions
try {
    $acl = Get-Acl $authorizedKeysPath
    $acl.SetAccessRuleProtection($true, $false)   # disable inheritance, remove inherited rules
    $systemRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        "SYSTEM", "FullControl", "Allow"
    )
    $adminRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        "Administrators", "FullControl", "Allow"
    )
    $acl.SetAccessRule($systemRule)
    $acl.SetAccessRule($adminRule)
    Set-Acl -Path $authorizedKeysPath -AclObject $acl -ErrorAction Stop
    Write-Info "Permissions set on authorized_keys (SYSTEM + Administrators only)."
} catch {
    Write-Info "Warning: Could not set strict permissions on authorized_keys: $_"
    Write-Info "If SSH key auth fails later, run: icacls $authorizedKeysPath /inheritance:r"
}

# Ensure sshd_config uses administrators_authorized_keys (uncomment if needed)
$sshdConfig = Join-Path $sshdDataDir "sshd_config"
if (Test-Path $sshdConfig) {
    $content = Get-Content $sshdConfig -Raw -Encoding utf8
    $adminKeyFileLine = "AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys"
    # Check if the active (non-commented) entry already exists
    if ($content -notmatch "(?m)^\s*AuthorizedKeysFile\s+__PROGRAMDATA__/ssh/administrators_authorized_keys") {
        $block = "`r`nMatch Group administrators`r`n       $adminKeyFileLine`r`n"
        Add-Content -Path $sshdConfig -Value $block -Encoding utf8
        Write-Info "sshd_config updated: administrators_authorized_keys block added."
    } else {
        Write-Info "sshd_config already configured for administrators_authorized_keys."
    }
    # Restart sshd to pick up config change
    try {
        Restart-Service sshd -ErrorAction Stop
        Write-Info "sshd restarted to apply config changes."
    } catch {
        Write-Info "Warning: Could not restart sshd. Config changes take effect on next restart."
    }
}

# ---------------------------------------------------------------------------
# Step 9: Enable WinRM
# ---------------------------------------------------------------------------

Write-Step "Enabling WinRM for Ansible remote management"

try {
    Enable-PSRemoting -Force -SkipNetworkProfileCheck -ErrorAction Stop | Out-Null
    Write-Info "PSRemoting enabled."
} catch {
    Write-Fatal "Failed to enable PSRemoting: $_"
}

try {
    Set-Item "WSMan:\localhost\Client\TrustedHosts" -Value $winrmRange -Force -ErrorAction Stop
    Write-Info "WinRM TrustedHosts set to: $winrmRange"
} catch {
    Write-Fatal "Failed to set WinRM TrustedHosts: $_"
}

try {
    Set-Item "WSMan:\localhost\Service\Auth\Basic" -Value $true -Force -ErrorAction Stop
    Write-Info "WinRM Basic authentication enabled."
} catch {
    Write-Fatal "Failed to enable WinRM Basic auth: $_"
}

Write-Log "WinRM configured: TrustedHosts=$winrmRange, Basic auth enabled."

# ---------------------------------------------------------------------------
# Step 10: Firewall rules — SSH (22) and WinRM (5985), Tailscale range only
# ---------------------------------------------------------------------------

Write-Step "Configuring firewall rules (ports 22 and 5985, Tailscale range only)"
$tailscaleRange = "100.64.0.0/10"

function Set-FleetFirewallRule {
    param(
        [string]$Name,
        [int]$Port,
        [string]$RemoteAddress
    )
    $existing = Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
    try {
        if ($existing) {
            # Update existing rule to ensure correct remote address
            Set-NetFirewallRule `
                -DisplayName $Name `
                -RemoteAddress $RemoteAddress `
                -ErrorAction Stop | Out-Null
            Write-Info "Updated rule: '$Name' (port $Port, source $RemoteAddress)"
        } else {
            New-NetFirewallRule `
                -DisplayName $Name `
                -Direction Inbound `
                -Protocol TCP `
                -LocalPort $Port `
                -RemoteAddress $RemoteAddress `
                -Action Allow `
                -ErrorAction Stop | Out-Null
            Write-Info "Created rule: '$Name' (port $Port, source $RemoteAddress)"
        }
    } catch {
        Write-Fatal "Failed to configure firewall rule '$Name': $_`nCheck that you are running as Administrator."
    }
}

Set-FleetFirewallRule -Name "Fleet - SSH (Tailscale)" -Port 22 -RemoteAddress $tailscaleRange
Set-FleetFirewallRule -Name "Fleet - WinRM (Tailscale)" -Port 5985 -RemoteAddress $tailscaleRange
Write-Log "Firewall rules set: SSH (22) and WinRM (5985), source $tailscaleRange"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

$hostname = $env:COMPUTERNAME
Write-Log "GCS enrollment complete | hostname=$hostname | tailscale_ip=$tailscaleIp"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  ENROLLMENT COMPLETE" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Hostname      : $hostname"
Write-Host "  Tailscale IP  : $tailscaleIp"
Write-Host "  SSH (port 22) : Key auth only, Tailscale range only"
Write-Host "  WinRM (5985)  : Tailscale range only"
Write-Host "  Log           : $LogFile"
Write-Host "  Device is enrolled and ready for remote Ansible management."
Write-Host "============================================================" -ForegroundColor Green
