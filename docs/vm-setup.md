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

## Bootstrap commands (root)

```bash
apt-get update -y
apt-get install -y ca-certificates curl gnupg lsb-release git docker.io docker-compose
systemctl enable --now docker
```

## Stack start

```bash
cd /opt/eos-webapp
docker-compose -f infra/docker-compose.yml up -d
```

## Expected local endpoints

- Frontend scaffold: `http://<vm-ip>:3000`
- Backend scaffold: `http://<vm-ip>:8080`
- EOS API docs: `http://<vm-ip>:8503/docs`
- EOS dashboard: `http://<vm-ip>:8504`
- Postgres: `<vm-ip>:5432`
```
