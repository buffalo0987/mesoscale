#!/usr/bin/env bash
# ==============================================================================
# Mesoscale — One-Click Automated Installer for Debian / Ubuntu Proxmox LXC
# ==============================================================================
set -euo pipefail

# Text formatting
BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo -e "${CYAN}${BOLD}"
echo "  __  __                               _       "
echo " |  \/  | ___  ___  ___  ___  ___ __ _| | ___  "
echo " | |\/| |/ _ \/ __|/ _ \/ __|/ __/ _\` | |/ _ \ "
echo " | |  | |  __/\__ \ (_) \__ \ (_| (_| | |  __/ "
echo " |_|  |_|\___||___/\___/|___/\___\__,_|_|\___| "
echo "      Adaptive Hyper-Local Weather Engine      "
echo -e "${RESET}"

# 1. Privilege Check
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo -e "${RED}[ERROR] This installer requires root privileges. Please run as root or install sudo.${RESET}"
        exit 1
    fi
else
    SUDO=""
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${MESOSCALE_INSTALL_DIR:-/opt/mesoscale}"
DATA_DIR="${MESOSCALE_DATA_DIR:-/var/lib/mesoscale/data}"
PORT="${MESOSCALE_PORT:-8080}"

# If cloned into /root or a temporary location, offer to move or copy to /opt/mesoscale
if [ "$SCRIPT_DIR" != "$TARGET_DIR" ]; then
    echo -e "${CYAN}[*] Installing repository into standard location: ${BOLD}${TARGET_DIR}${RESET}"
    $SUDO mkdir -p "$(dirname "$TARGET_DIR")"
    if [ ! -d "$TARGET_DIR" ]; then
        $SUDO cp -r "$SCRIPT_DIR" "$TARGET_DIR"
    else
        $SUDO rsync -a --exclude='.venv' --exclude='node_modules' "$SCRIPT_DIR/" "$TARGET_DIR/"
    fi
    INSTALL_DIR="$TARGET_DIR"
else
    INSTALL_DIR="$SCRIPT_DIR"
fi

cd "$INSTALL_DIR"

# 2. System Prerequisites
echo -e "${CYAN}[1/6] Installing system packages (curl, git, libgomp1, ca-certificates)...${RESET}"
$SUDO apt-get update -qq
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    curl \
    git \
    ca-certificates \
    libgomp1 \
    rsync \
    >/dev/null

# 3. Node.js 22 LTS (for compiling the React 19 PWA)
echo -e "${CYAN}[2/6] Checking Node.js environment...${RESET}"
NODE_NEEDS_INSTALL=false
if ! command -v node >/dev/null 2>&1; then
    NODE_NEEDS_INSTALL=true
else
    NODE_MAJOR="$(node -v | cut -d'.' -f1 | tr -d 'v')"
    if [ "$NODE_MAJOR" -lt 20 ]; then
        NODE_NEEDS_INSTALL=true
    fi
fi

if [ "$NODE_NEEDS_INSTALL" = true ]; then
    echo -e "      Installing Node.js 22 LTS via NodeSource..."
    curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO bash - >/dev/null 2>&1
    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs >/dev/null
fi
echo -e "${GREEN}      Node $(node -v) & npm $(npm -v) ready.${RESET}"

# 4. Astral uv (Python toolchain)
echo -e "${CYAN}[3/6] Checking uv (Python package manager)...${RESET}"
if ! command -v uv >/dev/null 2>&1; then
    echo -e "      Installing uv into /usr/local/bin..."
    curl -LsSf https://astral.sh/uv/install.sh | $SUDO env UV_INSTALL_DIR="/usr/local/bin" sh >/dev/null 2>&1
fi
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
echo -e "${GREEN}      $(uv --version) ready.${RESET}"

# 5. Compile Frontend PWA
echo -e "${CYAN}[4/6] Building React 19 + TypeScript + Tailwind v4 PWA bundle...${RESET}"
npm --prefix frontend --silent install --no-audit --no-fund
npm --prefix frontend run build --silent
$SUDO mkdir -p backend/mesoscale/static_dist
$SUDO cp -r frontend/dist/* backend/mesoscale/static_dist/
echo -e "${GREEN}      Frontend PWA assets compiled cleanly.${RESET}"

# 6. Python Virtual Environment Sync
echo -e "${CYAN}[5/6] Syncing Python dependencies (FastAPI, DuckDB, Polars, CatBoost, LightGBM)...${RESET}"
uv sync --no-dev
echo -e "${GREEN}      Python virtual environment synced.${RESET}"

# 7. Dedicated User, Data Directory, and Permissions
echo -e "${CYAN}[6/6] Configuring dedicated system service...${RESET}"
if ! id -u mesoscale >/dev/null 2>&1; then
    $SUDO useradd -r -s /bin/false -d /var/lib/mesoscale mesoscale
fi

$SUDO mkdir -p "$DATA_DIR"
$SUDO chown -R mesoscale:mesoscale "$DATA_DIR"
$SUDO chown -R mesoscale:mesoscale "$INSTALL_DIR"

# 8. Create & Enable Systemd Service
SERVICE_FILE="/etc/systemd/system/mesoscale.service"
$SUDO tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Mesoscale Self-Learning Weather Engine & PWA
After=network.target

[Service]
Type=simple
User=mesoscale
Group=mesoscale
WorkingDirectory=${INSTALL_DIR}
Environment="MESOSCALE_DATA_DIR=${DATA_DIR}"
Environment="PYTHONUNBUFFERED=1"
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn mesoscale.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips "*"
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now mesoscale

# 9. Verify Service Health
echo -e "      Waiting for Mesoscale server to boot on port ${PORT}..."
HEALTHY=false
for i in {1..20}; do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    sleep 1
done

IP_ADDR="$(hostname -I | awk '{print $1}')"

echo ""
if [ "$HEALTHY" = true ]; then
    echo -e "${GREEN}${BOLD}✓ Mesoscale installed and running successfully!${RESET}"
else
    echo -e "${YELLOW}${BOLD}! Service started, but healthcheck took longer than expected.${RESET}"
    echo -e "  Inspect logs with: ${BOLD}journalctl -u mesoscale -n 50${RESET}"
fi

echo ""
echo -e "${BOLD}Access Mesoscale on your local network:${RESET}"
echo -e "  → ${CYAN}http://${IP_ADDR}:${PORT}${RESET}"
echo ""
echo -e "${BOLD}Useful Commands:${RESET}"
echo -e "  • Check status:   ${CYAN}systemctl status mesoscale${RESET}"
echo -e "  • Live logs:      ${CYAN}journalctl -u mesoscale -f${RESET}"
echo -e "  • Restart server: ${CYAN}systemctl restart mesoscale${RESET}"
echo -e "  • Update app:     ${CYAN}${INSTALL_DIR}/update.sh${RESET}"
echo ""
echo -e "${YELLOW}${BOLD}Note on PWA, GPS & Push Notifications:${RESET}"
echo -e "Mobile browsers require HTTPS for geolocation, offline caching, and push."
echo -e "To access securely from your phone with a trusted Let's Encrypt certificate:"
echo -e "  1. Install Tailscale:  ${CYAN}curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up${RESET}"
echo -e "  2. Enable HTTPS serve: ${CYAN}sudo tailscale serve --bg ${PORT}${RESET}"
echo ""
