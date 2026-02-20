# Slice 3: EOS Orchestration + EOS Persistence + MQTT Trigger Output

## Scope

This slice adds end-to-end EOS orchestration on top of existing MQTT input mapping:

1. Detect EOS automatic runs via `/v1/health`.
2. Persist EOS artifacts in dedicated `eos_*` tables (raw + normalized).
3. Provide runtime/run APIs for the middle and right UI panes.
4. Publish output topics (`plan`, `solution`, resource command/preview) with safety gates and audit logs.

## Environment variables

Set these in `.env` (defaults shown in `.env.example`):

- `EOS_SYNC_POLL_SECONDS`
- `EOS_AUTOCONFIG_ENABLE`
- `EOS_AUTOCONFIG_MODE`
- `EOS_AUTOCONFIG_INTERVAL_SECONDS`
- `EOS_FORCE_RUN_TIMEOUT_SECONDS`
- `EOS_FORCE_RUN_ALLOW_LEGACY`
- `EOS_OUTPUT_MQTT_ENABLED`
- `EOS_OUTPUT_MQTT_PREFIX`
- `EOS_OUTPUT_MQTT_QOS`
- `EOS_OUTPUT_MQTT_RETAIN`
- `EOS_ACTUATION_ENABLED`

## Migration

Apply the migration that creates:

- `eos_runs`
- `eos_artifacts`
- `eos_prediction_points`
- `eos_plan_instructions`
- `eos_mqtt_output_events`
- `control_targets`

```bash
docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

## Runtime/API checks

```bash
curl -s http://192.168.3.157:8080/api/eos/runtime | jq
curl -s -X PUT http://192.168.3.157:8080/api/eos/runtime/config \
  -H "Content-Type: application/json" \
  -d '{"ems_mode":"OPTIMIZATION","ems_interval_seconds":900}' | jq
curl -s -X POST http://192.168.3.157:8080/api/eos/runs/force | jq
curl -s http://192.168.3.157:8080/api/eos/runs | jq
curl -s http://192.168.3.157:8080/api/eos/output-events | jq
```

## MQTT output contract

Published topics:

1. `${EOS_OUTPUT_MQTT_PREFIX}/plan/latest`
2. `${EOS_OUTPUT_MQTT_PREFIX}/solution/latest`
3. Per-resource command topic from `control_targets.command_topic`

Safety behavior:

- If `EOS_ACTUATION_ENABLED=false` or target is `dry_run_only=true`, publish to `<command_topic>/preview`.
- If `EOS_NO_GRID_CHARGE_GUARD_ENABLED=true`, battery charge commands are skipped when live `grid_import_w` exceeds `EOS_NO_GRID_CHARGE_GUARD_THRESHOLD_W`.
- Every publish or skip decision is written into `eos_mqtt_output_events`.

## Notes

- `gesamtlast` is not a standard scalar MQTT live input in automatic EOS mode.
- Collector treats missing plan/solution (`404`) as `partial` run status, not fatal.
- Force-run strategy is `pulse_then_legacy`:
  - First pulse EMS interval to `1`.
  - Wait for new `last_run_datetime`.
  - Optional legacy `/optimize` fallback if enabled and timeout is reached.
