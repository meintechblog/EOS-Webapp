# Slice 9: Konkrete Outputs aus EOS-Runs (HTTP Dispatch)

## Begriffe: `plan` vs `solution`

- `plan`: ausführbare Instruktionen je Resource und Zeit (`operation_mode_id`, `operation_mode_factor`, `execution_time`).
- `solution`: Optimierungsergebnis über den gesamten Horizont (Kosten, Energieflüsse, SOC, Faktoren).

Praktisch:

- Für Aktorik wird primär der `plan` verwendet.
- Für Plausibilitäts- und Kostenanalyse wird die `solution` verwendet.

## Normalisierung

Beim Persistieren von Plan-Instruktionen wird `execution_time` explizit übernommen:

1. `payload.execution_time`
2. Fallback: `effective_at`
3. Fallback: `start_datetime` / `starts_at`

Dadurch funktionieren aktive Zustände und Dispatch robust auch bei `starts_at = null`.

## HTTP Dispatch Engine

`OutputDispatchService` führt drei Modi aus:

1. `scheduled`: Zustandswechsel zum Slotzeitpunkt genau einmal.
2. `heartbeat`: periodisches Re-Senden des aktuell aktiven Zustands.
3. `force`: sofortiges Senden per API-Trigger.

Defaults:

- `OUTPUT_HTTP_DISPATCH_ENABLED=true`
- `OUTPUT_SCHEDULER_TICK_SECONDS=15`
- `OUTPUT_HEARTBEAT_SECONDS=60`

## Safety

No-grid-charge Guard blockiert Ladeinstruktionen bei Netzbezug:

- `EOS_NO_GRID_CHARGE_GUARD_ENABLED=true`
- `EOS_NO_GRID_CHARGE_GUARD_THRESHOLD_W=50`

Bei Block wird kein HTTP-Call ausgeführt; stattdessen Audit-Event mit `status=blocked`.

## Zieldefinitionen

Output-Ziele werden in `output_targets` gepflegt:

- `resource_id`
- `webhook_url`
- `method` (`POST|PUT|PATCH`)
- `headers_json`
- `enabled`
- `timeout_seconds`
- `retry_max`
- `payload_template_json`

## Audit

Jeder Dispatch-Versuch landet in `output_dispatch_events`:

- `dispatch_kind`: `scheduled|heartbeat|force`
- `status`: `sent|blocked|failed|retrying|skipped_no_target`
- `http_status`, `error_text`
- `idempotency_key`

## APIs

- `GET /api/eos/outputs/current`
- `GET /api/eos/outputs/timeline`
- `GET /api/eos/outputs/events`
- `POST /api/eos/outputs/dispatch/force`
- `GET /api/eos/runs/{id}/plausibility`
- `GET/POST/PUT /api/eos/output-targets`

## Schnelltest

```bash
curl -s http://127.0.0.1:8080/api/eos/outputs/current | jq
curl -s http://127.0.0.1:8080/api/eos/outputs/timeline | jq
curl -s -X POST http://127.0.0.1:8080/api/eos/outputs/dispatch/force \
  -H "Content-Type: application/json" -d '{"resource_ids":null}' | jq
```
