# EOS-Webapp

Local-first web application as an interface layer for [Akkudoktor-EOS](https://github.com/Akkudoktor-EOS/EOS).

## Status

Project bootstrap in progress (Sprint 0).

## Goals (v1)

- Single-user
- Local network deployment
- No login/auth (initially)
- MQTT-driven live inputs
- EOS optimization runs from web UI
- Persisted history for inputs/runs/results

## Planned architecture

- `frontend/` — 3-pane web UI (Inputs / Parameters+Run / Outputs)
- `backend/` — API, MQTT ingest, EOS orchestration
- `infra/` — docker compose, environment templates
- `docs/` — setup and runbooks

## Notes

Initial repository starts private; target is public release once stable and documented.
