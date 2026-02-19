# EOS-Webapp Worklog

## 2026-02-19
- Project skeleton created.
- Private GitHub repository connected and synced.
- Proxmox VM 702 provisioned (Debian 13.3).
- Docker + docker-compose installed on VM.
- Base stack deployed (eos, postgres, backend, frontend scaffold).
- Backend `/status` and live status pages implemented.
- Added `/stats/live` alias and server-rendered fallback.

## Open work
- EOS optimize orchestration API and right-pane outputs still pending.

## 2026-02-19 (Slice 1 implementation)
- Added backend configuration model and env wiring for DB/MQTT/stale threshold.
- Added SQLAlchemy + Alembic with initial migration for `input_mappings` and `telemetry_events`.
- Implemented mapping APIs: `GET/POST/PUT /api/mappings`.
- Implemented live values API: `GET /api/live-values` with `healthy/stale/never`.
- Implemented MQTT ingest service with dynamic subscription sync after mapping changes.
- Added payload parsing for scalar values and JSON dot-path extraction.
- Extended `/status` payload with `db`, `mqtt`, and `telemetry` sections.
- Added `.env.example`, compose env passthrough, README quickstart updates, and slice runbook.

## 2026-02-19 (Slice 2 implementation)
- Replaced frontend scaffold container with real frontend build (`React + Vite + TypeScript`) served by nginx.
- Added nginx proxy routing from frontend to backend for `/api`, `/health`, `/status`, and `/stats/*`.
- Implemented left pane:
  - mapping creation form
  - mapping list
  - per-mapping live value + last seen + `healthy/stale/never` status
  - inline mapping enable/disable action
- Added polling every 5 seconds against `GET /api/live-values`.
- Kept middle/right panes as structured placeholders for next slices.
- Added slice 2 runbook and README updates.
