# Slice 1 Runbook: MQTT -> DB -> API

This runbook covers the first working end-to-end slice:

`MQTT broker -> backend ingest -> Postgres telemetry -> live API`.

## Prerequisites

- Stack up via `docker-compose -f infra/docker-compose.yml up -d --build`
- Migrations applied via `docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head`
- MQTT broker reachable at `192.168.3.8:1883`

## API endpoints

- `GET /api/mappings`
- `POST /api/mappings`
- `PUT /api/mappings/{id}`
- `GET /api/live-values`
- `GET /status`

## 1) Create mapping

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

Expected:

- HTTP `201`
- response contains mapping `id` and configured fields

## 2) Test publish (scalar)

```bash
mosquitto_pub -h 192.168.3.8 -t eos/input/pv_power_w -m '1234'
```

Expected:

- one new telemetry row in `telemetry_events`
- `GET /api/live-values` returns `parsed_value: "1234"`

## 3) Test publish (JSON + payload_path)

Create another mapping:

```bash
curl -s -X POST http://192.168.3.157:8080/api/mappings \
  -H "Content-Type: application/json" \
  -d '{
    "eos_field":"pv_power_json",
    "mqtt_topic":"eos/input/pv_json",
    "payload_path":"sensor.power",
    "unit":"W",
    "enabled":true
  }' | jq
```

Publish JSON payload:

```bash
mosquitto_pub -h 192.168.3.8 -t eos/input/pv_json -m '{"sensor":{"power":987}}'
```

Expected:

- `parsed_value` stored as `"987"`

## 4) Check status integration

```bash
curl -s http://192.168.3.157:8080/status | jq
```

Expected sections:

- `db` with `ok: true`
- `mqtt` with connection + subscription info
- `telemetry` with `messages_received` and `last_message_ts`

## 5) Validate stale behavior

Default stale threshold is `LIVE_STALE_SECONDS=120`.

- if no new message arrives for > 120s, `GET /api/live-values` returns `status: "stale"`
- mappings without telemetry return `status: "never"`

