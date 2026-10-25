# NGS Server · Preview

Your game library on your own NAS. Windows clients download games over HTTP(S); source folders are mounted read-only. No SMB setup is required on the handheld.

## Docker

1. Install Docker Engine and Docker Compose on an x86-64 NAS or Linux PC.
2. Extract **NGS-Docker-0.1.0-preview.1.zip** into a permanent folder.
3. Copy `.env.example` to `.env`. Set `NGS_GAMES_PATH` to your existing games folder; optionally set `NGS_INSTALLERS_PATH`. Leave the empty-folder defaults to try NGS first.
4. Run `sh start.sh` in that folder and choose an admin password.
5. Wait until `docker compose ps` shows healthy services.
6. Open `http://NAS-IP:8890/assets/ngs_launcher/client/`.
7. Install the Windows client from GitHub Releases. In **Settings → Account**, enter `http://NAS-IP:8890/tactic`.
8. For the first local connection, copy **NGS local pairing code** from `docker compose logs app` into NGS. Register your player profile or sign in.

The server owner is **admin**, with the password you chose. Registration creates player accounts; it does not grant catalog or administration access. Use the owner account to add games with the catalog manager. The separate TACTIC administration login also uses `admin` and your chosen installation password.

The kit pulls `ghcr.io/listyque/nas-game-switcher/server:0.1.0-preview.1`, PostgreSQL 16.15 and Caddy 2.11.7. Keep its folder, `.env`, `data`, `secrets` and `backups`. Never publish these files. Do not share a Compose project name between installations.

## Synology DSM

1. Use an **x86-64 NAS**, **DSM 7.2.1+** and **Container Manager 20.10.23-1437+**.
2. In Package Center choose **Manual Install** and select **NGS-Synology-0.1.0-preview.1.spk**.
3. Choose your game folder, an unused port and an admin password. Installer folders are optional; domains can be left blank.
4. Wait for the package to start, then open **NAS Game Switcher** in DSM.
5. Install NGS on Windows, enter the displayed server address and copy the local pairing code from the app container log in Container Manager. Register or sign in.

The SPK includes offline server images and uses Synology's Docker Project Worker. It does not require an SSH session or Docker commands for installation. Game folders stay read-only. Physical DSM installation is still awaiting device validation; this is a preview package.

## Internet access, without VPN

A domain is optional for local use. Configure it before enabling Internet access:

1. Point a domain or DDNS name to your public IP. In `.env`, set `NGS_PUBLIC_DOMAIN` to that hostname only; on DSM, enter it in package setup.
2. Forward **WAN 80 → NAS 8088** and **WAN 443 → NAS 8448** (or your chosen gateway ports). Forward neither the internal NGS port nor PostgreSQL. Restrict the raw NGS port to the trusted LAN.
3. Run `sh start.sh` again. Caddy obtains a trusted HTTPS certificate. Ports 80/443 must be reachable for certificate validation; CGNAT requires a separate reachable endpoint.
4. Use `https://your-domain/tactic` in NGS. Open the same domain for the download page and administration.

HTTPS is required for Internet connections. The gateway checks the NGS owner profile before allowing the separate TACTIC administration login, rejects cross-site administration requests and protects game assets with player authorization. Local HTTP pairing verifies server identity but does not encrypt LAN traffic; use it only on a trusted network. Do not disable certificate verification.

## Updates and backups

Before updating Docker, run `sh backup.sh` and keep the resulting private backup safely. Update the image tag in `compose.yaml`, then run `sh start.sh`. Do not delete data volumes. On DSM install the newer public SPK over the existing package; saved folders, passwords and options are retained. A matching public bootstrap is required; private development databases are deliberately rejected. Before a changed server plugin starts, NGS saves both databases under `backups/before-update-*`; failed backups stop the update.

The Windows client uses signed official GitHub update manifests and verifies package hashes. Client updates do not depend on a particular NAS. Source builds: from this directory run `docker build -t ghcr.io/listyque/nas-game-switcher/server:0.1.0-preview.1 .`. The Dockerfile fetches a pinned, checksum-verified upstream TACTIC commit; no personal database is needed.
