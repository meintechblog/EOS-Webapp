# VM Setup (Proxmox) — EOS-Webapp

## Base image

We use Debian netinstall ISO:

- https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.3.0-amd64-netinst.iso

## Current VM baseline

- VMID: `702`
- Name: `eos-webapp`
- OS: Debian 13.3 (trixie)
- CPU: 4 vCPU
- RAM: 8 GB
- Disk: 40 GB
- Network: vmbr0

## Recommended bootstrap (script-first)

```bash
cd /opt/eos-webapp
sudo ./scripts/auto-install.sh
```

The script installs host dependencies (`curl`, `jq`, `ripgrep`, `docker.io`, docker compose plugin), starts the stack, runs migrations, and performs health checks.

## Manual bootstrap (fallback)

```bash
apt-get update -y
apt-get install -y ca-certificates curl jq ripgrep git docker.io docker-compose-plugin
systemctl enable --now docker

cd /opt/eos-webapp
cp .env.example .env
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml exec -T backend alembic upgrade head
```

## Expected local endpoints

- Frontend: `http://<vm-ip>:3000`
- Backend: `http://<vm-ip>:8080`
- EOS API docs: `http://<vm-ip>:8503/docs`
- EOS dashboard: `http://<vm-ip>:8504`
- Postgres: `<vm-ip>:5432`
