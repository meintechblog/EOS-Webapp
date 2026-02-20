#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker-compose.yml"

log() {
  printf '[auto-install] %s\n' "$*"
}

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    log "run as root: sudo ./scripts/auto-install.sh"
    exit 1
  fi
}

ensure_compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
    return
  fi
  log "docker compose command not found after install"
  exit 1
}

install_host_dependencies() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y \
    ca-certificates \
    curl \
    jq \
    ripgrep \
    git \
    docker.io \
    docker-compose-plugin
  systemctl enable --now docker
}

wait_for_http() {
  local url="$1"
  local attempts="${2:-40}"
  local sleep_seconds="${3:-2}"

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$sleep_seconds"
  done
  return 1
}

main() {
  need_root
  log "installing host dependencies"
  install_host_dependencies

  local compose_cmd
  compose_cmd="$(ensure_compose_cmd)"

  cd "$ROOT_DIR"

  if [[ ! -f "$ROOT_DIR/.env" ]]; then
    log "creating .env from .env.example"
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  fi

  log "building and starting stack"
  $compose_cmd -f "$COMPOSE_FILE" up -d --build

  log "running alembic migrations"
  $compose_cmd -f "$COMPOSE_FILE" exec -T backend alembic upgrade head

  log "waiting for backend health"
  if ! wait_for_http "http://127.0.0.1:8080/health" 60 2; then
    log "backend health check failed"
    exit 1
  fi

  log "checking /status"
  curl -fsS "http://127.0.0.1:8080/status" | jq '.status, .eos.health_ok, .output_dispatch.enabled'

  log "checking /api/eos/runtime"
  curl -fsS "http://127.0.0.1:8080/api/eos/runtime" | jq '{health_ok, collector: .collector.running}'

  log "done"
}

main "$@"
