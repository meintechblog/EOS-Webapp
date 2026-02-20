# Slice 2.5 Runbook: Automap + Mapping Management + Field Hints

Slice 2.5 extends the input pane with topic discovery, one-click automap, full mapping management, and clearer semantic guidance for ambiguous fields.

## What is included

- Discovery store for observed MQTT topics (`mqtt_topic_observations`)
- Mapping extensions:
  - `value_multiplier`
  - `sign_convention` (`canonical|unknown|positive_is_import|positive_is_export`)
- New APIs:
  - `DELETE /api/mappings/{id}`
  - `GET /api/discovered-topics`
  - `POST /api/mappings/automap`
- Discovery subscription always active on `MQTT_DISCOVERY_TOPIC` (default `eos/#`)
- Value transform in mapped ingest path:
  - numeric payload -> multiplier/sign applied
  - non-numeric payload -> stored unchanged, warning logged if transform is configured
- Frontend updates:
  - one-click `Automap`
  - mapping edit and delete
  - source selection (`MQTT topic` or `Fixed value`) in create/edit
  - field notes/info panel, including `grid_power_w` sign warning

## API quick checks

1. Discovered topics with suggestions:

```bash
curl -s http://192.168.3.157:8080/api/discovered-topics | jq
```

`GET /api/discovered-topics` is intentionally scoped to active `eos/input/` topics only.
Stale historical-only topics are filtered out using `MQTT_DISCOVERY_ACTIVE_SECONDS` (default `30`).

2. Apply automap immediately:

```bash
curl -s -X POST http://192.168.3.157:8080/api/mappings/automap | jq
```

Expected response sections:
- `created`
- `updated`
- `unchanged`
- `skipped`
- `normalizations`
- `warnings`

3. Delete mapping (and telemetry rows for that mapping):

```bash
curl -i -X DELETE http://192.168.3.157:8080/api/mappings/<id>
```

Expected: `204 No Content`.

## Prefix normalization behavior

- If observed topic is `eos/input/<x>`, automap keeps it unchanged.
- If observed topic is `eos/<x>`, automap normalizes to `eos/input/<x>`.
- If normalized target was not observed yet, automap returns warning:
  - switch publisher to `eos/input/<x>`.

## Additional topic synonyms

- `eos/input/battery_power_charge_kw` is now matched to `battery_power_w`
  with `value_multiplier=1000`.
- `battery_power_charging_kw`, `battery_power_discharge_kw`, and
  `battery_power_discharging_kw` are handled the same way.

## `payload_path` reminder

`payload_path` is optional and only needed for JSON payloads.

Example:
- payload: `{"sensor":{"power":987}}`
- path: `sensor.power`
- parsed value: `987`

Leave empty for scalar payloads such as `1234`.

## `grid_power_w` sign guidance

Canonical convention used by app docs:
- positive = grid import (Bezug)
- negative = export (Einspeisung)

Because installations vary, default for `grid_power_w` is `unknown` until explicitly set.

## Fixed-value mapping constraints

- A mapping must use exactly one source:
  - `mqtt_topic` or
  - `fixed_value`
- `payload_path` is only valid for MQTT mappings.
- Automap does not overwrite existing fixed-value mappings. These are returned as skipped with
  `field_has_fixed_value_manual_review`.

## Validation commands used in this slice

1. Build images:

```bash
docker-compose -f infra/docker-compose.yml build backend frontend
```

2. Start stack and migrate:

```bash
docker-compose -f infra/docker-compose.yml up -d postgres eos backend frontend
docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

3. Verify status:

```bash
curl -s http://192.168.3.157:8080/status | jq
```
