# Mesoscale — Deployment Guide (Proxmox LXC, Raspberry Pi & Tailscale HTTPS)

**Mesoscale** can be deployed in two convenient ways:
1. **[Proxmox LXC Automated Script (install.sh & update.sh)](INSTALL_LXC.md)** — Native bare-metal execution in a Debian/Ubuntu LXC with systemd and zero Docker overhead.
2. **Docker / Docker Compose** — Multi-arch (`linux/arm64` Raspberry Pi + `linux/amd64`) container.

Both pair directly with **Tailscale HTTPS** so iOS Safari and Android Chrome get a trusted Let's Encrypt certificate (`https://<host>.<tailnet>.ts.net`) for **PWA Home-Screen Installation**, **GPS Location**, **Service Worker Offline Caching**, and **Native Web Push Notifications**—with zero open router ports.

## 1. Quick Proxmox LXC Install
See the complete **[INSTALL_LXC.md](INSTALL_LXC.md)** guide:
```bash
apt update && apt install -y git
git clone <your-repo-url> /opt/mesoscale
cd /opt/mesoscale
./install.sh
```

## 2. Launch on Raspberry Pi / Home Server (Docker)

```bash
docker compose up -d --build
```

Notice `docker-compose.yml` binds `127.0.0.1:8080:8080` so the HTTP port isn't exposed raw on untrusted networks while `tailscale serve` proxies TLS cleanly.

## 2. Enable Tailscale HTTPS Proxy (1 Command)

Ensure HTTPS Certificates and MagicDNS are enabled in your [Tailscale Admin DNS settings](https://login.tailscale.com/admin/dns), then run on your Raspberry Pi / server:

```bash
sudo tailscale serve --bg 8080
```

Tailscale will print your permanent HTTPS URL, e.g.:
`https://raspberrypi.tail1234.ts.net/`

## 3. Install PWA & Enable Web Push on Your Phone

1. With Tailscale connected on your phone, open `https://<your-host>.<tailnet>.ts.net` in **Safari (iOS)** or **Chrome (Android)**.
2. **Add to Home Screen** (`Share -> Add to Home Screen` on iOS, or the `Install Mesoscale` banner on Android/Desktop).
3. Launch **Mesoscale** from your home screen:
   - Search your city/ZIP or tap **Use Current GPS**.
   - Tap **Pin & Train Local AI** to trigger the 365-day historical forecast + Mesonet/ASOS backfill and initial `CatBoost` + `LightGBM` MOS training (~30–60s).
   - Open **Settings (Gear Icon)** → **Enable Push Notifications** → tap **Send Test Push** to verify end-to-end VAPID delivery and customize Quiet Hours.

## 4. Local Development (Without Docker)

```bash
.venv/bin/uvicorn mesoscale.main:app --host 127.0.0.1 --port 8080 --reload
```
