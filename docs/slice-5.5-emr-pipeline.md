# Slice 5.5: EMR Pipeline (W -> kWh) + EOS Measurement Sync

## Scope

Slice 5.5 extends the existing data backbone with:

1. Persistent `power_samples` for relevant instantaneous power signals.
2. Monotonic `energy_emr` derivation (`kWh`) from source timestamps.
3. EOS measurement sync service (periodic + force endpoint) with audited run table.

## New env variables

- `EMR_ENABLED` (default `true`)
- `EMR_HOLD_MAX_SECONDS` (default `300`)
- `EMR_DELTA_MIN_SECONDS` (default `1`)
- `EMR_DELTA_MAX_SECONDS` (default `3600`)
- `EMR_POWER_MIN_W` (default `0`)
- `EMR_POWER_MAX_W` (default `50000`)
- `EMR_HOUSE_POWER_MAX_W` (default `60000`)
- `EMR_PV_POWER_MAX_W` (default `60000`)
- `EMR_GRID_POWER_MAX_W` (default `60000`)
- `EMR_BATTERY_POWER_MIN_W` (default `-25000`)
- `EMR_BATTERY_POWER_MAX_W` (default `25000`)
- `EMR_GRID_CONFLICT_THRESHOLD_W` (default `50`)
- `EOS_MEASUREMENT_SYNC_ENABLED` (default `true`)
- `EOS_MEASUREMENT_SYNC_SECONDS` (default `30`)
- `EOS_MEASUREMENT_SYNC_FORCE_TIMEOUT_SECONDS` (default `20`)

## Migration

Apply:

```bash
docker compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

Migration `20260220_0007_emr_pipeline.py` adds:

- `input_mappings.timestamp_path`
- `power_samples`
- `energy_emr`
- `eos_measurement_sync_runs`

## EMR rules

1. Input mapping:
- `house_load_w -> house_load_emr_kwh`
- `pv_power_w -> pv_production_emr_kwh`
- `grid_import_w -> grid_import_emr_kwh`
- `grid_export_w -> grid_export_emr_kwh`

2. Timestamp:
- Source timestamp from `timestamp_path` (JSON dot-path, ISO8601 or unix s/ms)
- Fallback to broker receive time

3. Integration:
- Hold-last integration for `delta <= EMR_HOLD_MAX_SECONDS`
- No integration for large gaps (`gap_no_integrate`) or implausible deltas (`delta_out_of_range`)
- EMR remains monotonic

4. Grid conflict:
- If import/export are both above `EMR_GRID_CONFLICT_THRESHOLD_W` at the same `ts`, the smaller side is clamped to `0`.

5. Battery sign:
- `battery_power_w` keeps signed semantics (`+` charging, `-` discharging) and is not converted to an EMR key.

## APIs

### Data

- `GET /api/data/power/latest`
- `GET /api/data/power/series?key=<power_key>&from=<iso>&to=<iso>`
- `GET /api/data/emr/latest`
- `GET /api/data/emr/series?emr_key=<emr_key>&from=<iso>&to=<iso>`

### EOS measurement sync

- `GET /api/eos/measurement-sync/status`
- `POST /api/eos/measurement-sync/force`

## Quick verification

```bash
curl -s http://192.168.3.157:8080/api/data/power/latest | jq
curl -s "http://192.168.3.157:8080/api/data/power/series?key=house_load_w" | jq

curl -s http://192.168.3.157:8080/api/data/emr/latest | jq
curl -s "http://192.168.3.157:8080/api/data/emr/series?emr_key=house_load_emr_kwh" | jq

curl -s http://192.168.3.157:8080/api/eos/measurement-sync/status | jq
curl -s -X POST http://192.168.3.157:8080/api/eos/measurement-sync/force | jq

curl -s http://192.168.3.157:8080/status | jq '.emr, .eos_measurement_sync'
```

## Notes

- Battery power is optional for measurement sync; if not listed in `measurement.keys`, it is skipped.
- Measurement sync also includes SOC aliases (`battery_soc_percent` and `battery_soc_pct`) when available.
- Strict parameter validation now checks `measurement.keys` and `measurement.*_emr_keys` consistency.
