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
- Left-pane frontend UI (mapping form + live values) not yet implemented.
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
