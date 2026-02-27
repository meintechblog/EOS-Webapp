# EOS-Webapp

Local-first web app as interface for Akkudoktor-EOS.

## Relationship to Akkudoktor-EOS

EOS-Webapp is a companion UI/orchestration layer that builds on top of
`Akkudoktor-EOS` APIs (prediction, optimization, runtime, and outputs).
It does not replace EOS core services and expects EOS to be running.

- EOS upstream repository: `https://github.com/Akkudoktor-EOS/EOS`
- EOS documentation: `https://docs.akkudoktor.net/akkudoktoreos/`

## Current mode (Slice 10)

The app runs in **HTTP-only setup + central HTTP output pull mode**.

- Left pane: `Inputs & Setup` (autosave, mandatory/optional/live, import/export)
  - now as categorized accordion with `MUSS`/`KANN`, base vs advanced fields, and repeatable add/remove for PV planes, E-Autos, Home-Appliances
- Middle pane: `Run-Center` (runtime, force run, full run history)
- Right pane: `Outputs` (active decisions, timeline, output-signals pull, plausibility)
- Collapsible sections in UI use a consistent triangle caret (same behavior in `Inputs & Setup`, output charts, and main output blocks).
- Right pane top includes a collapsible chart block (strompreis in `ct/kWh`, PV + load forecast in `kW`, mode/factor timelines)
  - Mode timeline now renders one bar chart per resource/device and uses the same time window as the other output charts.
- Dynamic field updates use **`/eos/set/*`**
- Output pull uses one central endpoint: **`/eos/get/outputs`**
  - default response is Loxone-friendly plain text (`signal_key:value` per line)
  - JSON debug/inspection via `?format=json`
  - Web UI panel `Output-Signale (HTTP Pull)` refreshes every **5s**
- Legacy MQTT/channel/mapping/control paths are removed from active API + runtime
- Legacy `POST /optimize` fallback reuses previous `start_solution` (warm-start) from persisted run artifacts when available
- Price prediction quality uses auto backfill checks against raw `prediction/series` history (not interpolated `prediction/list`)

Retention defaults:

- `DATA_RAW_RETENTION_DAYS=14`
- `DATA_ROLLUP_5M_RETENTION_DAYS=180`
- `DATA_ROLLUP_1H_RETENTION_DAYS=1095`

## UI Unit Contract (mandatory)

Operator-facing units are fixed:

- Prices: `ct/kWh`
- Capacity: `kWh`
- Power: `kW`
- Display precision for power in UI: max `3` decimals (`kW`)

Internal storage may still use EOS-compatible units (`EUR/kWh`, `Wh`, `W`) but conversion is transparent in UI/API setup flows.

Details:

- `docs/ui-unit-policy.md`
- `docs/eos-price-prediction-quality.md`
- `docs/eos-upstream-findings.md`
- `AGENTS.md`

Consistency check:

```bash
./scripts/check-ui-unit-policy.sh
```

CI guardrail:

- GitHub Actions workflow: `.github/workflows/ui-unit-policy.yml`

## Run-Center Horizon + Runtime

- Run-Center contains a prominent horizon dropdown.
- It writes `prediction.hours` and `prediction.historic_hours` (`672h` = 4 weeks, or less effectively if less history is available in DB/provider).
- If available in current EOS payload, it also writes `optimization.horizon_hours` (or legacy `optimization.hours`).
- Run artifact capture waits briefly for EOS plan/solution materialization after a run:
  - `EOS_RUN_ARTIFACT_WAIT_SECONDS=45`
  - `EOS_RUN_ARTIFACT_POLL_SECONDS=3`
- Run history shows live runtime for active runs and final duration for completed runs.
- Run history entries include per-run prediction metrics (target horizon from run context, effective horizon, historic horizon, point count, price range when available).
- Automatic price-history backfill:
  - `EOS_HTTP_TIMEOUT_SECONDS=20` (EOS API request timeout used by refresh/capture/backfill calls)
  - `EOS_PRICE_BACKFILL_ENABLED=true`
  - `EOS_PRICE_BACKFILL_TARGET_HOURS=672` (4 weeks)
  - `EOS_PRICE_BACKFILL_MIN_HISTORY_HOURS=648` (minimum acceptable raw coverage)
  - `EOS_PRICE_BACKFILL_COOLDOWN_SECONDS=86400` (max one restart-backfill per 24h)
  - `EOS_PRICE_BACKFILL_RESTART_TIMEOUT_SECONDS=180`
  - `EOS_PRICE_BACKFILL_SETTLE_SECONDS=90` (wait window after restart-refresh to allow provider history to materialize)
  - Triggered only on prediction refresh scopes `prices` / `all`.
  - Quality decision is based on raw `GET /v1/prediction/series?key=elecprice_marketprice_wh`.
  - `prediction/list` may look complete due interpolation and is treated as diagnostics only.

## Inputs & Setup 2.0

- `Inputs & Setup` is grouped into collapsible categories:
  - `Standort & Basis`
  - `PV & Forecast`
  - `Tarife & Last`
  - `Speicher & Inverter`
  - `E-Autos`
  - `Home-Appliances`
  - `Messwerte & EMR`
  - `Live-Signale`
- Mandatory categories are opened by default; optional categories are collapsed by default.
- Repeatables:
  - `PV-Planes` (Plane #1 base object, not deletable; additional planes deletable)
  - `E-Autos` (all optional, deletable)
  - `Home-Appliances` (all optional, deletable, structured time-window editor with add/remove)
- Add strategy: clone-first. If no clone source exists, backend template fallback is used.
- Autosave is unchanged and now applies to dynamic field ids as well.

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
http://<host-ip>:3000
```

In the API examples below, replace `<host-ip>` with `localhost` on the same machine.

## Quickstart (manual)

```bash
cd /opt/eos-webapp
cp .env.example .env
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml exec -T backend alembic upgrade head
```

## Core setup APIs

```bash
curl -s http://<host-ip>:8080/api/setup/fields | jq
curl -s http://<host-ip>:8080/api/setup/layout | jq
curl -s -X PATCH http://<host-ip>:8080/api/setup/fields \
  -H "Content-Type: application/json" \
  -d '{"updates":[{"field_id":"param.general.latitude","value":49.1128,"source":"ui"}]}' | jq
curl -s -X POST http://<host-ip>:8080/api/setup/entities/mutate \
  -H "Content-Type: application/json" \
  -d '{"action":"add","entity_type":"electric_vehicle","clone_from_item_key":"electric_vehicle:0"}' | jq
curl -s http://<host-ip>:8080/api/setup/readiness | jq
curl -s http://<host-ip>:8080/api/setup/export | jq
```

## HTTP setter contract (`/eos/set`)

Live signal:

```bash
curl -s "http://<host-ip>:8080/eos/set/signal/pv_power_kw=2.0" | jq
```

Parameter update:

```bash
curl -s "http://<host-ip>:8080/eos/set/param/general/latitude=49.1128" | jq
curl -s "http://<host-ip>:8080/eos/set/param/devices/batteries/lfp/max_soc_percentage?value=95" | jq
curl -s "http://<host-ip>:8080/eos/set/param/devices/batteries/lfp/capacity_kwh=90" | jq
curl -s "http://<host-ip>:8080/eos/set/param/elecprice/charges_ct_per_kwh=3.5" | jq
```

Accepted timestamp params: `ts` or `timestamp` (ISO8601 or unix s/ms).

## Run / output APIs

```bash
curl -s http://<host-ip>:8080/api/eos/runtime | jq
curl -s -X POST http://<host-ip>:8080/api/eos/runs/force | jq
curl -s http://<host-ip>:8080/api/eos/runs | jq
curl -s http://<host-ip>:8080/api/eos/runs/103/plan | jq
curl -s http://<host-ip>:8080/api/eos/runs/103/solution | jq
curl -s http://<host-ip>:8080/api/eos/runs/103/plausibility | jq
curl -s http://<host-ip>:8080/api/eos/outputs/current | jq
curl -s http://<host-ip>:8080/api/eos/outputs/timeline | jq
curl -s http://<host-ip>:8080/api/eos/output-signals | jq
curl -s http://<host-ip>:8080/eos/get/outputs
curl -s "http://<host-ip>:8080/eos/get/outputs?format=json" | jq
```

Loxone command identifiers from central pull are:

```bash
battery1_target_power_kw:\v
shaby_target_power_kw:\v
```

## Status endpoints

```bash
curl -s http://<host-ip>:8080/health
curl -s http://<host-ip>:8080/status | jq
```

## Archived docs

Historical MQTT-era slice docs were moved to `docs/archive/`.
