# Mesoscale — Proxmox LXC Automated Installation Guide

With the automated [`install.sh`](file:///home/michael/Documents/Weather%20App/install.sh) and [`update.sh`](file:///home/michael/Documents/Weather%20App/update.sh) scripts, deploying **Mesoscale** into a clean Proxmox LXC container takes just 3 commands.

---

## 1. Create a Debian 12 or Ubuntu 24.04 LXC in Proxmox

In the Proxmox VE Web UI:
1. Click **Create CT**.
2. **Template:** Debian 12 (standard) or Ubuntu 24.04.
3. **Disks:** 10–16 GB.
4. **CPU:** 2 Cores.
5. **Memory:** 1024 MB RAM (Mesoscale idles under 150 MB; 1 GB easily accommodates parallel ML training).
6. **Network:** DHCP or static IP on your local bridge (`vmbr0`).
7. **Unprivileged:** Default (Yes).

---

## 2. Run the One-Click Installer

Open the LXC container console and run:

```bash
apt update && apt install -y git
git clone <your-repository-url> /opt/mesoscale
cd /opt/mesoscale
./install.sh
```

*(If copying the code directly from this machine instead of git, you can run `rsync -avP --exclude '.venv' --exclude 'node_modules' . root@<LXC-IP>:/opt/mesoscale` and run `./install.sh`)*

### What `install.sh` Does Automatically:
1. Installs system build libraries (`curl`, `ca-certificates`, `libgomp1`, `git`).
2. Installs **Node.js 22 LTS** and compiles the **React 19 + TypeScript + Tailwind v4** PWA bundle.
3. Installs **Astral `uv`** and syncs Python 3.12 dependencies (`FastAPI`, `DuckDB`, `Polars`, `CatBoost`, `LightGBM`).
4. Creates an unprivileged system user `mesoscale` and persistent analytical storage `/var/lib/mesoscale/data`.
5. Configures, enables, and starts the `/etc/systemd/system/mesoscale.service` unit.
6. Waits for the healthcheck on port `8080` and displays your access URL (`http://<LXC-IP>:8080`).

---

## 3. Updating Mesoscale

Whenever you push updates or new model features, update the LXC in 1 command:

```bash
cd /opt/mesoscale
./update.sh
```

This pulls the latest commits, syncs Python dependencies, rebuilds frontend assets, restarts the systemd service, and verifies the `/api/health` endpoint.

---

## 4. Useful Service Commands

```bash
# View live application & forecast engine logs
journalctl -u mesoscale -f

# Check service status
systemctl status mesoscale

# Restart or stop
systemctl restart mesoscale
systemctl stop mesoscale
```

---

## 5. Mobile Access, PWA Install & Web Push (HTTPS)

Modern mobile browsers (iOS Safari & Android Chrome) require **HTTPS** for:
- One-tap GPS Geolocation
- Home-screen PWA installation
- Native VAPID Web Push notifications (NWS warnings, model shifts, Quiet Hours)

### Recommended: Tailscale inside the LXC (Zero open ports)

1. On the **Proxmox Host**, allow TUN access for your unprivileged LXC (replace `100` with your CT ID in `/etc/pve/lxc/100.conf`):
   ```properties
   lxc.cgroup2.devices.allow: c 10:200 rwm
   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
   ```
2. In the **LXC Console**, install and start Tailscale:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up
   ```
3. Enable instant HTTPS serving:
   ```bash
   sudo tailscale serve --bg 8080
   ```
   Tailscale prints your permanent Let's Encrypt HTTPS address:
   `https://<lxc-name>.<tailnet>.ts.net/`

Open that URL on your phone, tap **Add to Home Screen**, and enable push alerts!
