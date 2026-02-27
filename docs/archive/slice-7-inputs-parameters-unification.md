# Slice 7: Inputs + Parameters unification

## Goal
Slice 7 adds a unified operator flow:
- Left pane: `Inputs & Setup` with tabs
  - `Einrichtung`
  - `Live Inputs`
  - `Dynamische Parameter`
- Middle pane: `Run-Center`
- Right pane: `Outputs`

It also adds dynamic parameter ingest (`/eos/param/*`) on top of existing live input ingest (`/eos/input/*`).

## New environment variables
- `PARAM_DYNAMIC_ENABLED=true`
- `PARAM_DYNAMIC_APPLY_DEBOUNCE_SECONDS=2`
- `PARAM_DYNAMIC_ALLOW_HTTP=true`
- `PARAM_DYNAMIC_ALLOW_MQTT=true`
- `SETUP_CHECK_LIVE_STALE_SECONDS=120`

## Migration
Migration file:
- `backend/alembic/versions/20260220_0009_parameter_dynamic_unification.py`

Adds:
- `parameter_bindings`
- `parameter_input_events`
- `dynamic_input` revision source support
- `param_input` source type support in `signal_measurements_raw`

## Backend APIs

### Dynamic parameter catalog/bindings
- `GET /api/parameters/dynamic-catalog`
- `GET /api/parameter-bindings`
- `POST /api/parameter-bindings`
- `PUT /api/parameter-bindings/{id}`
- `DELETE /api/parameter-bindings/{id}`
- `GET /api/parameter-bindings/events`

### Dynamic parameter ingest
- `GET /eos/param/{channel_or_path:path}`
- `POST /api/input/param/push`

Supported GET examples:
- `/eos/param/lfp/max_soc_pct=95`
- `/eos/param/lfp/max_soc_pct?value=95`

### Setup + discovery/status
- `GET /api/setup/checklist`
- `GET /api/discovered-inputs?namespace=all|input|param`
- `/status` includes:
  - `parameters_dynamic`
  - `setup`

## Runtime behavior
- Dynamic events are audited in `parameter_input_events`.
- On accepted updates, backend writes draft revisions with source `dynamic_input`.
- Debounced auto-apply updates EOS config (`/v1/config`, `/v1/config/file`) via existing profile apply flow.
- MQTT runtime extends subscriptions with enabled dynamic parameter bindings.

## Frontend behavior

### Inputs & Setup tabs
- `Einrichtung`:
  - setup checklist card
  - parameter management shortcut
- `Live Inputs`:
  - existing input channels, mappings, automap, configured mappings
- `Dynamische Parameter`:
  - dynamic catalog
  - binding CRUD (create/edit/delete)
  - binding event audit
  - discovered parameter-input list

Tab and collapse state persistence uses localStorage.

## Quick verification

1. Build and start:

```bash
docker-compose -f infra/docker-compose.yml up -d --build
docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

2. Create a binding:

```bash
curl -s -X POST http://192.168.3.157:8080/api/parameter-bindings \
  -H "Content-Type: application/json" \
  -d '{
    "parameter_key":"devices.batteries[].max_soc_percentage",
    "selector_value":"lfp",
    "channel_id":2,
    "input_key":"eos/param/lfp/max_soc_pct",
    "value_multiplier":1,
    "enabled":true
  }' | jq
```

3. Push value via HTTP:

```bash
curl -s "http://192.168.3.157:8080/eos/param/lfp/max_soc_pct=95" | jq
```

4. Check audit + setup:

```bash
curl -s "http://192.168.3.157:8080/api/parameter-bindings/events?limit=20" | jq
curl -s "http://192.168.3.157:8080/api/setup/checklist" | jq
curl -s "http://192.168.3.157:8080/status" | jq '.parameters_dynamic, .setup'
```
