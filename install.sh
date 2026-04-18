#!/usr/bin/env bash
# Cruise Control — online installer
# Usage:  curl -fsSL https://raw.githubusercontent.com/mcglothi/cruise-control/main/install.sh | sudo bash
# Or:     sudo bash install.sh [--iface <interface>] [--port <port>]
set -euo pipefail

REPO="mcglothi/cruise-control"
INSTALL_PORT="${CRUISE_PORT:-8090}"
FORCE_IFACE=""

# ── Parse arguments ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --iface)  FORCE_IFACE="$2"; shift 2 ;;
    --port)   INSTALL_PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: sudo bash install.sh [--iface <interface>] [--port <port>]"
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ── Colour helpers ─────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; RESET=''
fi

info()    { echo -e "  ${CYAN}•${RESET} $*"; }
success() { echo -e "  ${GREEN}✓${RESET} $*"; }
warn()    { echo -e "  ${YELLOW}!${RESET} $*"; }
fatal()   { echo -e "  ${RED}✗${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── Banner ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}  ╔══════════════════════════════╗"
echo -e "  ║      CRUISE CONTROL          ║"
echo -e "  ║  GB10 Bandwidth Throttle     ║"
echo -e "  ╚══════════════════════════════╝${RESET}"
echo ""

# ── Pre-flight checks ──────────────────────────────────────────────────────────
header "Checking requirements..."

[[ "$EUID" -eq 0 ]] || fatal "This installer must be run as root. Try: sudo bash install.sh"

OS=$(uname -s)
[[ "$OS" == "Linux" ]] || fatal "Cruise Control requires Linux (detected: $OS)"

ARCH=$(dpkg --print-architecture 2>/dev/null || uname -m)
case "$ARCH" in
  arm64|aarch64) DEB_ARCH="arm64" ;;
  amd64|x86_64)  DEB_ARCH="amd64" ;;
  *) fatal "Unsupported architecture: $ARCH (arm64 and amd64 only)" ;;
esac
success "Architecture: $DEB_ARCH"

# Require dpkg (Debian/Ubuntu)
command -v dpkg >/dev/null 2>&1 || fatal "dpkg not found — Cruise Control requires a Debian/Ubuntu system"
success "Package manager: dpkg"

# Require curl or wget
if command -v curl >/dev/null 2>&1; then
  FETCH="curl"
  success "Downloader: curl"
elif command -v wget >/dev/null 2>&1; then
  FETCH="wget"
  success "Downloader: wget"
else
  fatal "Neither curl nor wget found — install one and retry"
fi

# Check for python3-flask dependency
if ! python3 -c "import flask" 2>/dev/null; then
  warn "python3-flask not found — will install automatically via apt"
fi

# ── Detect network interface ───────────────────────────────────────────────────
header "Detecting network interface..."

if [[ -n "$FORCE_IFACE" ]]; then
  IFACE="$FORCE_IFACE"
  info "Using specified interface: $IFACE"
else
  IFACE=$(ip route | awk '$1=="default"{print $5}' | head -1)
  if [[ -z "$IFACE" ]]; then
    warn "Could not auto-detect interface — will default to enP7s7"
    warn "Run: sudo nano /etc/systemd/system/cruise-control.service"
    warn "And set THROTTLE_IFACE=<your-interface>"
    IFACE="enP7s7"
  else
    success "Detected interface: $IFACE"
    LINK_SPEED=$(cat "/sys/class/net/${IFACE}/speed" 2>/dev/null || echo "unknown")
    [[ "$LINK_SPEED" != "unknown" ]] && info "Link speed: ${LINK_SPEED} Mbps"
  fi
fi

# ── Fetch latest release ───────────────────────────────────────────────────────
header "Fetching latest release..."

API_URL="https://api.github.com/repos/${REPO}/releases/latest"
if [[ "$FETCH" == "curl" ]]; then
  RELEASE_JSON=$(curl -fsSL "$API_URL") || fatal "Could not reach GitHub API — check internet connection"
else
  RELEASE_JSON=$(wget -qO- "$API_URL") || fatal "Could not reach GitHub API — check internet connection"
fi

VERSION=$(echo "$RELEASE_JSON" | grep -oP '"tag_name":\s*"v?\K[^"]+' | head -1)
[[ -n "$VERSION" ]] || fatal "Could not determine latest version — check GitHub releases"
success "Latest version: $VERSION"

DEB_NAME="cruise-control_${VERSION}_${DEB_ARCH}.deb"
DEB_URL=$(echo "$RELEASE_JSON" | grep -oP '"browser_download_url":\s*"\K[^"]+' \
          | grep "${DEB_ARCH}.deb" | head -1)
[[ -n "$DEB_URL" ]] || fatal "No ${DEB_ARCH} package found in release ${VERSION}"

# ── Download ───────────────────────────────────────────────────────────────────
header "Downloading package..."

TMP_DEB="/tmp/${DEB_NAME}"
info "Downloading $DEB_NAME..."
if [[ "$FETCH" == "curl" ]]; then
  curl -fsSL --progress-bar -o "$TMP_DEB" "$DEB_URL" || fatal "Download failed"
else
  wget -q --show-progress -O "$TMP_DEB" "$DEB_URL" || fatal "Download failed"
fi
success "Downloaded to $TMP_DEB"

# ── Install dependencies ───────────────────────────────────────────────────────
header "Installing dependencies..."

if command -v apt-get >/dev/null 2>&1; then
  apt-get install -y -q python3-flask iproute2 kmod 2>&1 \
    | grep -v "^$" | sed 's/^/  /' || true
  success "Dependencies installed"
else
  warn "apt-get not found — skipping dependency install"
  warn "Ensure python3-flask, iproute2, and kmod are installed"
fi

# ── Install package ────────────────────────────────────────────────────────────
header "Installing cruise-control ${VERSION}..."

dpkg -i "$TMP_DEB" || fatal "Package installation failed"
rm -f "$TMP_DEB"

# The postinst already enabled and started the service using the auto-detected
# interface. If the user passed --iface, patch it in now.
if [[ -n "$FORCE_IFACE" ]]; then
  SERVICE=/etc/systemd/system/cruise-control.service
  sed -i "s|THROTTLE_IFACE=.*|THROTTLE_IFACE=${FORCE_IFACE}|" "$SERVICE"
  systemctl daemon-reload
  systemctl restart cruise-control
fi

# Patch port if non-default
if [[ "$INSTALL_PORT" != "8090" ]]; then
  SERVICE=/etc/systemd/system/cruise-control.service
  if grep -q "THROTTLE_PORT" "$SERVICE" 2>/dev/null; then
    sed -i "s|THROTTLE_PORT=.*|THROTTLE_PORT=${INSTALL_PORT}|" "$SERVICE"
  else
    sed -i "/^\[Service\]/a Environment=THROTTLE_PORT=${INSTALL_PORT}" "$SERVICE"
  fi
  systemctl daemon-reload
  systemctl restart cruise-control
fi

# ── Verify ─────────────────────────────────────────────────────────────────────
header "Verifying..."

sleep 1
STATUS=$(systemctl is-active cruise-control 2>/dev/null || echo "unknown")
if [[ "$STATUS" == "active" ]]; then
  success "Service is running"
else
  warn "Service status: $STATUS"
  warn "Check logs with: sudo journalctl -u cruise-control -n 30"
fi

HOST_IP=$(hostname -I | awk '{print $1}')
UI_URL="http://${HOST_IP}:${INSTALL_PORT}"

echo ""
echo -e "${GREEN}${BOLD}  ✓ Cruise Control ${VERSION} installed successfully${RESET}"
echo ""
echo -e "  ${BOLD}Web UI:${RESET}      ${CYAN}${UI_URL}${RESET}"
echo -e "  ${BOLD}Interface:${RESET}   ${IFACE}"
echo -e "  ${BOLD}Config:${RESET}      /opt/cruise-control/config.json"
echo -e "  ${BOLD}Service:${RESET}     systemctl {start|stop|restart|status} cruise-control"
echo -e "  ${BOLD}Logs:${RESET}        journalctl -u cruise-control -f"
echo ""
echo -e "  To uninstall: ${YELLOW}sudo dpkg -r cruise-control${RESET}"
echo ""
