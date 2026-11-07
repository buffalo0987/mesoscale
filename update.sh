#!/usr/bin/env bash
# ==============================================================================
# Mesoscale — One-Click Updater for Debian / Ubuntu Proxmox LXC
# ==============================================================================
set -euo pipefail

BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo -e "${CYAN}${BOLD}[*] Updating Mesoscale...${RESET}"

# Privilege Check
export DEBIAN_FRONTEND=noninteractive

if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo -e "${RED}[ERROR] This updater requires root privileges. Please run as root or with sudo.${RESET}"
        exit 1
    fi
else
    SUDO=""
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${MESOSCALE_DATA_DIR:-/var/lib/mesoscale/data}"
PORT="${MESOSCALE_PORT:-8080}"
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"

cd "$INSTALL_DIR"

# 1. Pull Latest Git Code
if [ -d ".git" ]; then
    echo -e "${CYAN}[1/4] Pulling latest git updates...${RESET}"
    if ! git config --global --get-all safe.directory 2>/dev/null | grep -Fxq "$INSTALL_DIR"; then
        git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    fi
    git -c safe.directory="$INSTALL_DIR" pull --ff-only || {
        echo -e "${YELLOW}Warning: git pull --ff-only failed. Retrying normal pull...${RESET}"
        git -c safe.directory="$INSTALL_DIR" pull
    }
else
    echo -e "${YELLOW}[1/4] No .git directory found. Skipping git pull.${RESET}"
fi

# 2. Sync Python Environment
echo -e "${CYAN}[2/4] Syncing Python dependencies with uv...${RESET}"
uv sync --no-dev

# 3. Rebuild Frontend Assets
echo -e "${CYAN}[3/4] Rebuilding React 19 PWA frontend...${RESET}"
npm --prefix frontend --silent install --no-audit --no-fund
npm --prefix frontend run build --silent
$SUDO rm -rf backend/mesoscale/static_dist
$SUDO mkdir -p backend/mesoscale/static_dist
$SUDO cp -r frontend/dist/* backend/mesoscale/static_dist/

# 4. Permissions & Service Restart
echo -e "${CYAN}[4/4] Restarting Mesoscale systemd service...${RESET}"
if id -u mesoscale >/dev/null 2>&1; then
    $SUDO chown -R mesoscale:mesoscale "$INSTALL_DIR"
    if [ -d "$DATA_DIR" ]; then
        $SUDO chown -R mesoscale:mesoscale "$DATA_DIR"
    fi
fi

$SUDO systemctl restart mesoscale

# 5. Verify Healthcheck
echo -e "      Waiting for service to become healthy..."
HEALTHY=false
for i in {1..15}; do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    sleep 1
done

if [ "$HEALTHY" = true ]; then
    echo -e "${GREEN}${BOLD}✓ Mesoscale updated and running smoothly!${RESET}"
else
    echo -e "${YELLOW}${BOLD}! Service restarted, but health check is still responding.${RESET}"
    echo -e "  Check logs: ${BOLD}journalctl -u mesoscale -n 50${RESET}"
fi
