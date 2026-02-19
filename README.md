# EOS-Webapp

Local-first web application as an interface layer for [Akkudoktor-EOS](https://github.com/Akkudoktor-EOS/EOS).

## Status

Sprint 1 slice 1 implemented: MQTT ingest, DB persistence, mapping API, live values API.

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

## Quickstart (local VM)

1. Create env file:

```bash
cd /opt/eos-webapp
cp .env.example .env
```

2. Start stack:

```bash
docker-compose -f infra/docker-compose.yml up -d --build
```

3. Apply migrations:

```bash
docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

4. Validate backend:

```bash
curl -s http://192.168.3.157:8080/health
curl -s http://192.168.3.157:8080/status | jq
```

## Slice 1 API test flow

1. Create mapping:

```bash
curl -s -X POST http://192.168.3.157:8080/api/mappings \
  -H "Content-Type: application/json" \
  -d '{
    "eos_field":"pv_power_w",
    "mqtt_topic":"eos/input/pv_power_w",
    "unit":"W",
    "enabled":true
  }' | jq
```

2. Publish test message:

```bash
mosquitto_pub -h 192.168.3.8 -t eos/input/pv_power_w -m '1234'
```

3. Read live values:

```bash
curl -s http://192.168.3.157:8080/api/live-values | jq
```

4. Check status page:

```bash
curl -s http://192.168.3.157:8080/status | jq
```

Then open `http://192.168.3.157:8080/status/live` in your browser.

## Notes

- Initial repository starts private; target is public release once stable and documented.
- For detailed slice runbook and examples see `docs/slice-1-mqtt-db-api.md`.
