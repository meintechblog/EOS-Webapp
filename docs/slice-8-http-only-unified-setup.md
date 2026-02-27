# Slice 8: HTTP-Only Unified Inputs & Setup

## Scope

Slice 8 switches the app to a simplified HTTP-only setup model:

- One unified setup API (`/api/setup/*`) for required, optional, and live fields.
- One HTTP setter contract (`/eos/set/*`) for dynamic overrides.
- Autosave UX in frontend (debounce + onBlur), no explicit field save buttons.
- Legacy MQTT/channel/mapping/binding/control APIs removed from active runtime.

## New primary endpoints

- `GET /api/setup/fields`
- `PATCH /api/setup/fields`
- `GET /api/setup/layout`
- `POST /api/setup/entities/mutate`
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

## Removed APIs (hard removed)

- `/api/input-channels*`
- `/api/mappings*`
- `/api/discovered-inputs`
- `/api/discovered-topics`
- `/api/parameter-bindings*`
- `/api/parameter-bindings/events`
- `/api/live-values`
- `/api/setup/checklist`
- `/api/eos/output-events`
- `/api/eos/control-targets*`

## Hard reset migration

Migration `20260221_0010_http_only_unified_setup.py`:

- creates `setup_field_events`
- truncates legacy input configuration/state tables
- normalizes parameter profile state to one active profile (`Current`) and one retained revision

Hard cleanup migrations:

- `20260221_0012_http_only_hard_cleanup.py`
  - removes remaining MQTT/channel/mapping/binding/control DB tables
  - removes `mapping_id` from `signal_measurements_raw` and `power_samples`
  - normalizes `signal_measurements_raw.source_type` away from `mqtt_input`
- `20260221_0013_prediction_storage_tuning.py`
  - drops legacy `eos_prediction_points`
  - keeps only chart-relevant `prediction.*` signal series in signal backbone

## UI behavior

- No tabs in left pane. Inputs are shown as categorized accordion with `MUSS`/`KANN` status and category-level validation summary.
- Categories:
  - Standort & Basis
  - PV & Forecast
  - Tarife & Last
  - Speicher & Inverter
  - E-Autos
  - Home-Appliances
  - Messwerte & EMR
  - Live-Signale
- Default open state: categories with required fields are open; purely optional categories are collapsed.
- Repeatable add/remove (clone-first, template fallback):
  - PV planes (base plane not deletable)
  - E-Autos
  - Home-Appliances
  - Home-Appliance time windows
- Mandatory fields stay red until server confirms valid saved state.
- Each field shows fixed HTTP path template and HTTP trigger active/inactive indicator.
