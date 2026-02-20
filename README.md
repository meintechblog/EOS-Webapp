# EOS-Webapp

Local-first web app as interface for Akkudoktor-EOS.

## Current mode (Slice 9)

The app runs in **HTTP-only setup + HTTP output dispatch mode**.

- Left pane: `Inputs & Setup` (autosave, mandatory/optional/live, import/export)
- Middle pane: `Run-Center` (runtime, force run, full run history)
- Right pane: `Outputs` (active decisions, timeline, dispatch log, output targets, plausibility)
- Dynamic field updates use **`/eos/set/*`**
- Output dispatch uses **HTTP webhooks** (scheduled + heartbeat + force)
- MQTT dispatch path stays disabled in active runtime path

## UI Unit Contract (mandatory)

Operator-facing units are fixed:

- Prices: `ct/kWh`
- Capacity: `kWh`
- Power: `kW`

Internal storage may still use EOS-compatible units (`EUR/kWh`, `Wh`, `W`) but conversion is transparent in UI/API setup flows.

Details:

- `docs/ui-unit-policy.md`
- `AGENTS.md`

Consistency check:

```bash
./scripts/check-ui-unit-policy.sh
```

CI guardrail:

- GitHub Actions workflow: `.github/workflows/ui-unit-policy.yml`

## Dependencies

Backend Python dependencies are in `backend/requirements.txt` (including `pandas==2.2.3`).

Host/tooling dependencies are installed by `scripts/auto-install.sh`:

- `curl`
- `jq`
- `ripgrep`
- `git`
- `docker.io`
- `docker compose` plugin

## Script-first setup

```bash
cd /opt/eos-webapp
sudo ./scripts/auto-install.sh
```

The script:

1. installs host dependencies,
2. builds/starts compose stack,
3. runs `alembic upgrade head`,
4. checks `/health`, `/status`, `/api/eos/runtime`.

Open UI:

```text
http://192.168.3.157:3000
```

## Quickstart (manual)

```bash
cd /opt/eos-webapp
cp .env.example .env
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml exec -T backend alembic upgrade head
```

## Core setup APIs

```bash
curl -s http://192.168.3.157:8080/api/setup/fields | jq
curl -s -X PATCH http://192.168.3.157:8080/api/setup/fields \
  -H "Content-Type: application/json" \
  -d '{"updates":[{"field_id":"param.general.latitude","value":49.1128,"source":"ui"}]}' | jq
curl -s http://192.168.3.157:8080/api/setup/readiness | jq
curl -s http://192.168.3.157:8080/api/setup/export | jq
```

## HTTP setter contract (`/eos/set`)

Live signal:

```bash
curl -s "http://192.168.3.157:8080/eos/set/signal/pv_power_kw=2.0" | jq
```

Parameter update:

```bash
curl -s "http://192.168.3.157:8080/eos/set/param/general/latitude=49.1128" | jq
curl -s "http://192.168.3.157:8080/eos/set/param/devices/batteries/lfp/max_soc_percentage?value=95" | jq
curl -s "http://192.168.3.157:8080/eos/set/param/devices/batteries/lfp/capacity_kwh=90" | jq
curl -s "http://192.168.3.157:8080/eos/set/param/elecprice/charges_ct_per_kwh=3.5" | jq
```

Accepted timestamp params: `ts` or `timestamp` (ISO8601 or unix s/ms).

## Run / output APIs

```bash
curl -s http://192.168.3.157:8080/api/eos/runtime | jq
curl -s -X POST http://192.168.3.157:8080/api/eos/runs/force | jq
curl -s http://192.168.3.157:8080/api/eos/runs | jq
curl -s http://192.168.3.157:8080/api/eos/runs/103/plan | jq
curl -s http://192.168.3.157:8080/api/eos/runs/103/solution | jq
curl -s http://192.168.3.157:8080/api/eos/runs/103/plausibility | jq
curl -s http://192.168.3.157:8080/api/eos/outputs/current | jq
curl -s http://192.168.3.157:8080/api/eos/outputs/timeline | jq
curl -s http://192.168.3.157:8080/api/eos/outputs/events | jq
curl -s -X POST http://192.168.3.157:8080/api/eos/outputs/dispatch/force \
  -H "Content-Type: application/json" -d '{"resource_ids":null}' | jq
```

## Output target management

```bash
curl -s http://192.168.3.157:8080/api/eos/output-targets | jq
curl -s -X POST http://192.168.3.157:8080/api/eos/output-targets \
  -H "Content-Type: application/json" \
  -d '{
    "resource_id":"lfp",
    "webhook_url":"http://192.168.3.20:9000/eos/dispatch",
    "method":"POST",
    "enabled":true,
    "timeout_seconds":10,
    "retry_max":2,
    "headers_json":{},
    "payload_template_json":null
  }' | jq
```

## Status endpoints

```bash
curl -s http://192.168.3.157:8080/health
curl -s http://192.168.3.157:8080/status | jq
```
