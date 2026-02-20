# Slice 8: HTTP-Only Unified Inputs & Setup

## Scope

Slice 8 switches the app to a simplified HTTP-only setup model:

- One unified setup API (`/api/setup/*`) for required, optional, and live fields.
- One HTTP setter contract (`/eos/set/*`) for dynamic overrides.
- Autosave UX in frontend (debounce + onBlur), no explicit field save buttons.
- MQTT input/output dispatch disabled in active runtime path.
- Legacy channel/mapping/automap/binding APIs marked as `410 Gone`.

## New primary endpoints

- `GET /api/setup/fields`
- `PATCH /api/setup/fields`
- `GET /api/setup/readiness`
- `GET /api/setup/export`
- `POST /api/setup/import`
- `POST /api/setup/set`
- `GET /eos/set/{path}`

Examples:

```bash
curl -s "http://localhost:8080/eos/set/signal/pv_power_kw=2.0" | jq
curl -s "http://localhost:8080/eos/set/param/general/latitude=49.1128" | jq
curl -s "http://localhost:8080/eos/set/param/devices/batteries/lfp/max_soc_percentage?value=95" | jq
```

## Compatibility aliases

- `GET /eos/input/*` -> mapped to `signal/*`
- `GET /eos/param/*` -> mapped to `param/*`

## Removed APIs (`410 Gone`)

- `/api/input-channels*`
- `/api/mappings/automap`
- `/api/discovered-inputs`
- `/api/discovered-topics`
- `/api/parameter-bindings*`
- `/api/parameter-bindings/events`
- `/api/setup/checklist`

## Hard reset migration

Migration `20260221_0010_http_only_unified_setup.py`:

- creates `setup_field_events`
- truncates legacy input configuration/state tables
- normalizes parameter profile state to one active profile (`Current`) and one retained revision

## UI behavior

- No tabs in left pane. Sections are:
  - Pflichtfelder
  - Optionale Felder
  - Live-Signale
  - Settings Import / Export
- Mandatory fields stay red until server confirms valid saved state.
- Each field shows fixed HTTP path template and HTTP trigger active/inactive indicator.

