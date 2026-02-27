# Slice 5: Data Backbone (Retention + Reproducibility + Fast Queries)

## Scope

Slice 5 introduces an additive data backbone:

1. Dual-write ingest (`telemetry_events` + `signal_measurements_raw`).
2. Latest-state upsert (`signal_state_latest`) for fast operational reads.
3. Reproducibility snapshots per EOS run (`eos_run_input_snapshots`).
4. Rollup + retention jobs with DB-audited executions.
5. Analytics APIs for signal catalog/latest/series + retention status.

## New environment variables

- `DATA_RAW_RETENTION_DAYS` (default `35`)
- `DATA_ROLLUP_5M_RETENTION_DAYS` (default `400`)
- `DATA_ROLLUP_1H_RETENTION_DAYS` (default `1825`)
- `DATA_ROLLUP_1D_RETENTION_DAYS` (default `0`, unlimited)
- `EOS_ARTIFACT_RAW_RETENTION_DAYS` (default `180`)
- `DATA_ROLLUP_JOB_SECONDS` (default `300`)
- `DATA_RETENTION_JOB_SECONDS` (default `3600`)

## Migration

Apply:

```bash
docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

Migration `20260220_0006_signal_backbone.py` adds:

- `signal_catalog`
- `signal_measurements_raw` (range-partitioned by month via `ts`)
- `signal_state_latest`
- `eos_run_input_snapshots`
- `signal_rollup_5m`
- `signal_rollup_1h`
- `signal_rollup_1d`
- `retention_job_runs`
- additive columns on `eos_mqtt_output_events`: `output_kind`, `resource_id`

## API overview

- `GET /api/data/signals`
- `GET /api/data/latest`
- `GET /api/data/series?signal_key=...&resolution=raw|5m|1h|1d`
- `GET /api/data/retention/status`
- `GET /api/eos/runs/{id}/context`

## Runtime behavior

1. MQTT ingest writes to:
- `telemetry_events` (legacy-compatible path)
- `signal_measurements_raw` (`source_type=mqtt_input`)
- `signal_state_latest` upsert

2. Fixed-value mappings write to:
- `signal_measurements_raw` (`source_type=fixed_input`)
- on create/update enable and backend startup seed

3. EOS collector writes to:
- `signal_measurements_raw` for prediction/plan/solution signals
- `eos_run_input_snapshots` once per run

4. Background jobs:
- rollup every `DATA_ROLLUP_JOB_SECONDS`
- retention every `DATA_RETENTION_JOB_SECONDS`
- each run audited in `retention_job_runs`

## Quick verification

```bash
curl -s http://<host-ip>:8080/api/data/signals | jq
curl -s "http://<host-ip>:8080/api/data/latest?limit=5" | jq
curl -s "http://<host-ip>:8080/api/data/series?signal_key=battery_power_w&resolution=raw" | jq
curl -s "http://<host-ip>:8080/api/data/retention/status" | jq
curl -s "http://<host-ip>:8080/status" | jq '.data_pipeline'
```

Run-context check:

```bash
curl -s http://<host-ip>:8080/api/eos/runs/9/context | jq
```

Expected:
- existing slice endpoints remain functional
- `data_pipeline` section available in `/status`
- `series` endpoint returns raw/rollup points by resolution
