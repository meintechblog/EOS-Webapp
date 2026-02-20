# Slice 2 Runbook: Left Pane UI

Note: this document covers the initial left-pane implementation. For discovery/automap/edit-delete extensions, see `docs/slice-2.5-automap.md`.

Slice 2 introduces the first real frontend with a 3-pane layout and a functional left pane for input mappings and live telemetry.

## URL

- Frontend UI: `http://192.168.3.157:3000`
- Backend API (direct): `http://192.168.3.157:8080`

The frontend container proxies `/api`, `/health`, `/status`, and `/stats/*` to the backend service.

## Implemented in this slice

- Mapping create form with source selection:
  - MQTT source (`eos_field`, `mqtt_topic`, optional `payload_path`, `unit`, `enabled`)
  - Fixed source (`eos_field`, `fixed_value`, `unit`, `enabled`)
  - EOS field dropdown from `GET /api/eos-fields` (EOS OpenAPI + measurement keys + fallback fields)
  - unit dropdown with field-aware suggestions
  - custom fallback for both field and unit
- Mapping list
- Per-mapping live telemetry card:
  - current value
  - last seen
  - status (`healthy`, `stale`, `never`)
- Polling every 5 seconds against `GET /api/live-values`
- Inline enable/disable toggle (uses `PUT /api/mappings/{id}`)
- Middle and right panes as structured placeholders for next slices

## Quick verification

1. Ensure stack is running:

```bash
docker-compose -f infra/docker-compose.yml up -d --build
docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

2. Create a mapping in UI or API:

```bash
curl -s -X POST http://192.168.3.157:8080/api/mappings \
  -H "Content-Type: application/json" \
  -d '{"eos_field":"pv_power_w","mqtt_topic":"eos/input/pv_power_w","unit":"W","enabled":true}' | jq
```

3. Publish MQTT value:

```bash
mosquitto_pub -h 192.168.3.8 -t eos/input/pv_power_w -m '1234'
```

4. Open UI and validate:

- mapping card exists
- value updates to `1234`
- status flips to `healthy`
- after inactivity > 120s status becomes `stale`

## Notes on `payload_path`

`payload_path` is used only for JSON payloads.

Example:

- payload: `{\"sensor\":{\"power\":987}}`
- `payload_path`: `sensor.power`
- stored `parsed_value`: `987`

If payload is a plain scalar (for example `1234`), keep `payload_path` empty.

## Notes on fixed-value mappings

Use fixed-value mappings for static EOS parameters that should not come from MQTT.

Example:

- `eos_field`: `einspeiseverguetung_euro_pro_wh`
- `fixed_value`: `0.00012`
- `mqtt_topic`: not used (`null`)

These mappings are shown in live values as `healthy` and do not require MQTT subscription.
