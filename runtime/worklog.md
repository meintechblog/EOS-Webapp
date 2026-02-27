# EOS-Webapp Worklog

## 2026-02-19
- Project skeleton created.
- Private GitHub repository connected and synced (initial state; repository is now public).
- Proxmox VM 702 provisioned (Debian 13.3).
- Docker + docker-compose installed on VM.
- Base stack deployed (eos, postgres, backend, frontend scaffold).
- Backend `/status` and live status pages implemented.
- Added `/stats/live` alias and server-rendered fallback.

## Open work
- EOS optimize orchestration API and right-pane outputs still pending.

## 2026-02-19 (Slice 1 implementation)
- Added backend configuration model and env wiring for DB/MQTT/stale threshold.
- Added SQLAlchemy + Alembic with initial migration for `input_mappings` and `telemetry_events`.
- Implemented mapping APIs: `GET/POST/PUT /api/mappings`.
- Implemented live values API: `GET /api/live-values` with `healthy/stale/never`.
- Implemented MQTT ingest service with dynamic subscription sync after mapping changes.
- Added payload parsing for scalar values and JSON dot-path extraction.
- Extended `/status` payload with `db`, `mqtt`, and `telemetry` sections.
- Added `.env.example`, compose env passthrough, README quickstart updates, and slice runbook.

## 2026-02-19 (Slice 2 implementation)
- Replaced frontend scaffold container with real frontend build (`React + Vite + TypeScript`) served by nginx.
- Added nginx proxy routing from frontend to backend for `/api`, `/health`, `/status`, and `/stats/*`.
- Implemented left pane:
  - mapping creation form
  - mapping list
  - per-mapping live value + last seen + `healthy/stale/never` status
  - inline mapping enable/disable action
- Added polling every 5 seconds against `GET /api/live-values`.
- Kept middle/right panes as structured placeholders for next slices.
- Added slice 2 runbook and README updates.

## 2026-02-19 (Slice 2 UX update)
- Added backend endpoint `GET /api/eos-fields` to provide EOS field catalog from EOS OpenAPI + measurement keys (+ fallback set).
- Updated left-pane `New Mapping` form:
  - EOS field is now a dropdown with custom fallback option.
  - Unit is now a field-aware dropdown with custom fallback option.
  - Added inline help text explaining `payload_path` behavior with JSON example.

## 2026-02-19 (Slice 2.5 implementation)
- Added migration `20260219_0002`:
  - new table `mqtt_topic_observations`
  - new `input_mappings` fields `value_multiplier` and `sign_convention`
  - sign-convention check constraint and backfill defaults
- Added discovery persistence and APIs:
  - `GET /api/discovered-topics`
  - continuous discovery subscribe via `MQTT_DISCOVERY_TOPIC` (`eos/#` default)
- Added one-click automap API:
  - `POST /api/mappings/automap` with `created/updated/unchanged/skipped/normalizations/warnings`
  - normalization rule `eos/<x>` -> `eos/input/<x>`
  - in-place field-based updates and conflict-safe skip handling
- Extended mapping management:
  - `DELETE /api/mappings/{id}` (hard delete + telemetry cascade)
  - `POST/PUT/GET /api/mappings` now include `value_multiplier` and `sign_convention`
- Extended ingest transform:
  - numeric multiplier and sign application on mapped values
  - `positive_is_export` inversion support
  - warning-only behavior for non-numeric payloads under transform
- Extended frontend input pane:
  - Automap button and result summary panel
  - discovered topics list with suggestions/confidence/notes
  - full mapping edit and delete flows
  - dedicated info hints for `payload_path`, units, and `grid_power_w` sign ambiguity
- Added docs for operations and verification:
  - `docs/archive/slice-2.5-automap.md`
  - README updates for Slice 2.5 flow
- Verified end-to-end scenarios on running stack:
  - automap apply + in-place update behavior
  - conflict skip with `topic_conflict_manual_review`
  - delete endpoint (`204`) with telemetry cascade in DB
  - sign inversion for `positive_is_export`
  - prefix normalization warning for old `eos/<field>` topics

## 2026-02-19 (Slice 2.5 refresh + grid semantics follow-up)
- Discovery listing now returns only active `eos/input/` topics (time-window filtered) to avoid stale historical entries in refresh view.
- Automap now supports `grid_power_consumption_kw` as synonym for `grid_power_w`.
- Grid sign handling now auto-uses `positive_is_import` when topic naming indicates import/consumption.
- UI automap list now highlights already-configured items with green card styling.
- UI info text now documents `eos/input/grid_power_consumption_kw` semantics: positive=Bezug, negative=Einspeisung.

## 2026-02-19 (Slice 2.5 fixed-value mapping support)
- Extended `input_mappings` to support static mappings via `fixed_value` and nullable `mqtt_topic`.
- Added DB constraint that enforces exactly one source per mapping (`mqtt_topic` XOR `fixed_value`).
- Updated mapping APIs and schemas to accept and return `fixed_value`.
- Updated live-values aggregation to emit fixed mappings as always-healthy values (derived from `fixed_value`).
- Updated automap to skip fields that already use fixed-value source, preventing accidental overwrite.
- Extended UI create/edit flows with source type selector (`MQTT topic` or `Fixed value`).
- Added field-level guidance for static tariff fields such as `einspeiseverguetung_euro_pro_wh`.

## 2026-02-19 (Automap update for battery charge topic rename)
- Extended automap synonym matching to recognize `battery_power_charge_kw` (and charging/discharging variants) as `battery_power_w` with multiplier `1000`.
- Verified discovery suggestion now returns `suggested_eos_field=battery_power_w` for `eos/input/battery_power_charge_kw`.
- Verified one-click automap can apply the new mapping target topic in the running stack.

## 2026-02-19 (Tariff fixed-value UX refinement)
- Added `EUR/kWh` as selectable unit for tariff fields (`einspeiseverguetung_euro_pro_wh`) in EOS field unit suggestions and frontend defaults.
- Updated `New Mapping` form: `Sign convention` is now hidden for fixed-value source and automatically sent as `canonical`.
- Updated `Configured Mappings` edit form with the same source-aware sign-convention behavior.
- Updated mapping card metadata so sign convention is not shown for fixed-value mappings.

## 2026-02-19 (Value display formatting)
- Updated frontend value rendering to format numeric values with standard decimal output and suppress scientific notation for small numbers.
- This improves readability for tariff-converted values such as `0.00009` (instead of `9e-05`).

## 2026-02-19 (Unit-aware multiplier preselection)
- Added frontend multiplier inference from selected unit and eos_field in `New Mapping`:
  - `_w`: `W -> 1`, `kW -> 1000`
  - `_wh`: `Wh -> 1`, `kWh -> 1000`
  - `*_euro_pro_wh`: `EUR/Wh -> 1`, `EUR/kWh -> 0.001`, `ct/kWh -> 0.00001`
- Field selection now auto-applies a matching multiplier when unit defaults are preselected.
- Edit form now mirrors this behavior when `eos_field` or `unit` is changed.

## 2026-02-19 (Fixed mapping status badge)
- Updated configured mapping cards to hide runtime status badge for fixed-value mappings (no `HEALTHY` badge).

## 2026-02-19 (EOS field help texts)
- Added curated, user-focused help for OpenAPI optimization fields in `GET /api/eos-fields`.
- Replaced raw OpenAPI wording with practical descriptions for:
  - `gesamtlast`
  - `pv_prognose_wh`
  - `strompreis_euro_pro_wh`
  - `einspeiseverguetung_euro_pro_wh`
  - `preis_euro_pro_wh_akku`
- Added actionable notes (array vs scalar expectations, live-vs-forecast guidance, and unit/multiplier hints).

## 2026-02-19 (Edit form unit+multiplier UX)
- Extended `Configured Mappings -> Edit` to use the same field-aware unit select as `New Mapping` (including custom unit fallback).
- Added automatic `value_multiplier` preselection on unit changes in edit flow, consistent with new-mapping behavior.

## 2026-02-19 (Slice 3 orchestration + EOS persistence + MQTT output)
- Added EOS orchestration persistence schema migration `20260219_0004`:
  - `eos_runs`
  - `eos_artifacts`
  - `eos_prediction_points`
  - `eos_plan_instructions`
  - `eos_mqtt_output_events`
  - `control_targets`
- Implemented backend EOS runtime stack:
  - `EosClient` service for EOS API calls (`health`, `config`, `prediction`, `plan`, `solution`, `optimize` fallback)
  - `EosOrchestratorService` collector loop for automatic run detection via `last_run_datetime`
  - force-run strategy `pulse_then_legacy`
  - raw + normalized run artifact persistence
  - MQTT output publishing (plan/solution/resource command-preview) with audit entries
- Extended backend APIs with additive `/api/eos/*` endpoints:
  - runtime + runtime config
  - force-run + run history/details + run plan/solution
  - output-events audit
  - control-target CRUD (`GET/POST/PUT`)
- Extended app lifecycle wiring:
  - initialize/start/stop EOS orchestrator in FastAPI lifespan
  - `/status` now includes EOS + collector sections and config snapshot fields
- Added MQTT publish helper in ingest service for output pipeline.
- Extended frontend middle/right panes:
  - EOS runtime card
  - EMS config editor
  - force-run action
  - run history + run artifact summary
  - plan/solution view
  - instruction list
  - MQTT output event log
  - control target create/edit
- Updated `.env.example` and `infra/docker-compose.yml` with Slice-3 env passthrough.
- Added docs:
  - `docs/archive/slice-3-eos-orchestration.md`
  - README Slice-3 runbook/API examples.

## 2026-02-19 (Slice 4 parameters profiles + import/export)
- Added parameters persistence schema migration `20260219_0005_parameters_profiles.py`:
  - `parameter_profiles`
  - `parameter_profile_revisions`
  - unique + partial-unique constraints for current draft and last applied revisions.
- Added backend services/repositories/APIs for full parameters profile workflow:
  - catalog: `GET /api/parameters/catalog`
  - profile CRUD/list/detail
  - draft save
  - strict validate
  - strict apply to EOS (`PUT /v1/config` + `PUT /v1/config/file`)
  - export (`masked/full`) and strict import preview/apply
- Added EOS settings validation stack based on EOS OpenAPI `SettingsEOS` schema + domain checks:
  - unknown field/type/range handling
  - cross-field checks (SOC min/max, inverter battery references)
  - provider checks against live EOS providers
  - sensitive field masking + masked-placeholder detection.
- Extended `/status` with additive `parameters` snapshot fields (`active_profile_*`, draft/applied revisions, last apply ts/error).
- Implemented full middle-pane Parameters UI:
  - tabbed pane (`Parameters` + `Runtime & Runs`)
  - profile select/create/activate
  - core forms for Standort, PV/Planes, Geräte, Tarife/Last
  - advanced JSON editor
  - actions: draft save, validate, apply
  - import/export panel with strict preview diff and apply into existing profile
  - legacy fixed-mapping overlap hint (Parameters leading for EOS Automatic).
- Verified with container builds and API smoke tests:
  - frontend build (`tsc + vite`) succeeds
  - backend image build succeeds
  - alembic upgrade to `0005` succeeds
  - `catalog/profiles/create/validate/export/import preview/apply/status.parameters` endpoints respond as expected.

## 2026-02-19 (Slice 5 data backbone)
- Added migration `20260220_0006_signal_backbone.py`:
  - `signal_catalog`
  - `signal_measurements_raw` (range-partitioned by `ts`)
  - `signal_state_latest`
  - `eos_run_input_snapshots`
  - `signal_rollup_5m`, `signal_rollup_1h`, `signal_rollup_1d`
  - `retention_job_runs`
  - additive `eos_mqtt_output_events.output_kind` + `resource_id`.
- Added new config/env knobs for retention/rollup scheduling and artifact retention.
- Implemented dual-write ingest pipeline:
  - MQTT mapped events: `telemetry_events` + `signal_measurements_raw` + latest-state upsert
  - fixed mappings on create/update-enable and startup seed into `signal_measurements_raw`
  - EOS collector writes prediction/plan/solution signals into backbone tables.
- Added full run context snapshot capture (`eos_run_input_snapshots`) and API `GET /api/eos/runs/{id}/context`.
- Added new Data APIs:
  - `GET /api/data/signals`
  - `GET /api/data/latest`
  - `GET /api/data/series`
  - `GET /api/data/retention/status`
- Implemented `DataPipelineService` background jobs:
  - periodic rollup upsert (5m/1h/1d)
  - periodic retention deletes (raw + rollups + eos raw artifacts)
  - job audit in `retention_job_runs`.
- Extended `/status` with additive `data_pipeline` metrics and added Slice-5 docs.

### Slice-5 verification
- `python3 -m compileall backend/app` and `backend/alembic/versions` passed.
- Backend image build + migration upgrade to `20260220_0006` passed.
- Smoke tests passed for:
  - `/api/data/signals`
  - `/api/data/latest`
  - `/api/data/series` (raw + 5m)
  - `/api/data/retention/status`
  - `/status.data_pipeline`
  - `/api/eos/runs/{id}/context` (200 after force run).

## 2026-02-19 (Slice 5.5 EMR pipeline + EOS measurement sync)
- Added migration `20260220_0007_emr_pipeline.py`:
  - `input_mappings.timestamp_path`
  - `power_samples`
  - `energy_emr`
  - `eos_measurement_sync_runs`
- Added EMR config/runtime parameters:
  - `EMR_*` integration thresholds and plausibility bounds
  - `EOS_MEASUREMENT_SYNC_*` periodic/force sync controls
- Extended MQTT ingest:
  - source timestamp extraction via `timestamp_path` (ISO8601 or unix s/ms)
  - fallback to broker receive timestamp
  - mapped power values now feed `power_samples` + EMR derivation service
- Implemented EMR derivation service:
  - monotonic kWh state in `energy_emr`
  - hold-last integration window
  - gap/no-integrate behavior
  - grid import/export same-timestamp conflict netting
  - EMR dual-write into `signal_measurements_raw` as derived signals
- Implemented EOS measurement sync service:
  - periodic background sync + force endpoint
  - preflight checks against `measurement.keys` and `measurement.*_emr_keys`
  - audit persistence in `eos_measurement_sync_runs`
- Added new data APIs:
  - `GET /api/data/power/latest`
  - `GET /api/data/power/series`
  - `GET /api/data/emr/latest`
  - `GET /api/data/emr/series`
- Added new EOS APIs:
  - `GET /api/eos/measurement-sync/status`
  - `POST /api/eos/measurement-sync/force`
- Extended `/status` with:
  - `emr`
  - `eos_measurement_sync`
- Extended parameter domain validation/catalog:
  - catalog includes measurement/EMR key fields
  - strict checks for `measurement.keys` and `measurement.*_emr_keys`
  - warnings when defaults differ from pipeline default keys
- Frontend updates:
  - New Mapping/Edit now support `Timestamp path`
  - Parameters core includes measurement/EMR key CSV fields
  - Outputs pane shows latest W, latest EMR, and measurement sync status/force action
- Documentation updates:
  - `docs/slice-5.5-emr-pipeline.md`
  - README Slice-5.5 section and test commands

### Slice-5.5 verification
- `python3 -m compileall backend/app backend/alembic/versions` passed.
- `docker compose build backend frontend` passed (frontend `tsc` + `vite build` succeeded).
- `alembic upgrade head` applied `20260220_0007`.
- API smoke tests passed for:
  - `/api/data/power/latest`
  - `/api/data/power/series`
  - `/api/data/emr/latest`
  - `/api/data/emr/series`
  - `/api/eos/measurement-sync/status`
  - `/api/eos/measurement-sync/force`
  - `/status` sections `.emr` and `.eos_measurement_sync`

## 2026-02-19 (Post Slice-5.5 hardening: signed battery power + SOC sync alias + grid conflict threshold)
- Installed `jq` on VM for compact OpenAPI/config inspection and faster JSON debugging.
- Hardened EMR power normalization in `EmrPipelineService`:
  - kept `battery_power_w` signed semantics (`+` charging, `-` discharging)
  - added dedicated battery clamps (`EMR_BATTERY_POWER_MIN_W`, `EMR_BATTERY_POWER_MAX_W`)
  - added per-domain max limits for house/PV/grid (`EMR_HOUSE_POWER_MAX_W`, `EMR_PV_POWER_MAX_W`, `EMR_GRID_POWER_MAX_W`)
- Updated grid import/export conflict handling:
  - conflict now triggers only if both values are above `EMR_GRID_CONFLICT_THRESHOLD_W` (default 50 W)
  - smaller side is clamped to `0` and note is persisted (`grid_conflict_resolved_keep_import|export`)
- Extended EOS measurement sync payload assembly:
  - includes SOC from `signal_state_latest` for `battery_soc_pct`
  - emits alias `battery_soc_percent` from the same latest value when missing
- Extended fallback/synonym handling:
  - automap synonym support for `battery_soc_percent`/`battery_soc_percentage`
  - EOS field catalog includes `battery_soc_percent` alias note
  - canonical unit inference includes `_percent`
- Extended `/status.config` with new EMR limit/threshold fields.

### Verification (post-hardening)
- `python3 -m compileall backend/app` passed.
- Rebuilt backend container: `docker compose -f infra/docker-compose.yml up -d --build backend`.
- Verified signed battery sample is persisted unmodified:
  - published `-1.25` on `eos/input/battery_power_charge_kw`
  - observed `power_samples.value_w=-1250.0` via `GET /api/data/power/series?key=battery_power_w`.
- Verified force sync includes SOC alias keys in warnings (as expected while measurement config is unset):
  - `measurement.keys does not contain 'battery_soc_pct'`
  - `measurement.keys does not contain 'battery_soc_percent'`.

## 2026-02-19 (Dispatch safety: no grid charging guard)
- Added output dispatch guard in `EosOrchestratorService`:
  - new settings: `EOS_NO_GRID_CHARGE_GUARD_ENABLED` (default `true`), `EOS_NO_GRID_CHARGE_GUARD_THRESHOLD_W` (default `50`)
  - battery charge-like commands are skipped when latest `grid_import_w` exceeds threshold
  - skip decisions are audited as `publish_status=skipped_grid_charge_guard`
- Added runtime visibility in `/status.config` for new guard settings.
- Added docs updates in README and `docs/archive/slice-3-eos-orchestration.md`.
- Updated parameter catalog hints for tariff providers to reflect current EOS provider set (`FeedInTariffFixed`/`FeedInTariffImport`) and import-series usage guidance.

### Dispatch guard verification
- Static helper smoke in container:
  - `charge + grid_import_w=1200` => blocked `True`
  - `charge + grid_import_w=10` => blocked `False`
  - `discharge + grid_import_w=1200` => blocked `False`
- `/status` confirms guard config values are active.

## 2026-02-19 (Hallbude parameter import rescue + strict-schema mapping)
- Received Hallbude profile package that failed strict import due schema/key mismatches (unknown fields in devices/pvforecast/feed-in import metadata and measurement.keys conflict).
- Built mapped import package for profile `3` (`Hallbude`) with EOS-compatible field names and units:
  - devices:
    - `id -> device_id`
    - `capacity_kwh -> capacity_wh` (x1000)
    - `soc_min_percent/soc_max_percent -> min_soc_percentage/max_soc_percentage`
    - `ac_power_limit_w -> max_power_w`
    - removed unsupported fields (`name`, `max_discharge_power_w`, `power_sign_convention`, `allow_grid_charging`)
  - pvforecast plane:
    - `azimuth -> surface_azimuth`
    - `tilt -> surface_tilt`
    - `mounting -> mountingplace`
    - `losses -> loss`
    - removed unsupported `name`
  - tariffs import settings:
    - converted object metadata to schema-compatible `import_json` string for
      `elecprice.elecpriceimport` and `feedintariff.provider_settings.FeedInTariffImport`
  - measurement:
    - set EMR arrays and explicit `measurement.keys` list for sync preflight compatibility.
- Found and fixed backend validation bug:
  - strict unknown-field sanitizer rejected `measurement.keys` while domain checks required it.
  - Added strict-mode extension schema acceptance for `measurement.keys` in `EosSettingsValidationService`.
- Result:
  - `POST /api/parameters/profiles/3/import/preview` => `valid=true`
  - `POST /api/parameters/profiles/3/import/apply` created new draft revision (`rev=2`, `source=import`)
  - `POST /api/parameters/profiles/3/validate` => `valid=true`

## 2026-02-20 (Slice 6: Multi-Channel Inputs MQTT+HTTP)
- Added migration `20260220_0008_input_channels_http_ingest.py`:
  - new tables: `input_channels`, `input_observations`
  - extended `input_mappings` with `channel_id`
  - replaced topic uniqueness with channel-aware `(channel_id, mqtt_topic)` unique index
  - replaced mapping source constraint with explicit channel-vs-fixed rules
  - extended `signal_measurements_raw` source type check with `http_input`
  - backfilled `mqtt-default`/`http-default`, mapped existing MQTT mappings, migrated legacy observations
- Extended ORM models:
  - `InputChannel`, `InputObservation`
  - channel relations/properties on `InputMapping` (`channel_code`, `channel_type`, `input_key`)
- Added channel CRUD repository + API:
  - `GET/POST/PUT/DELETE /api/input-channels`
  - delete guard with `409` when mappings still reference a channel
  - masked `password` field in channel API response
- Added shared ingest pipeline (`InputIngestPipelineService`):
  - shared flow for MQTT + HTTP
  - observation upsert -> mapping resolve -> parse/transform -> telemetry + signal_backbone persist -> EMR handoff
- Replaced single MQTT ingest runtime with channel-based worker runtime:
  - multiple MQTT clients (per enabled MQTT channel)
  - channel-specific config (`host`, `port`, `client_id`, `qos`, `discovery_topic`, optional auth)
  - dynamic worker resync on mapping/channel changes
- Added HTTP ingest API:
  - `GET /eos/input/{path:path}`
  - `POST /api/input/http/push`
  - supports both GET forms:
    - `/eos/input/pv_power_kw=2.0`
    - `/eos/input/pv_power_kw?value=2.0`
  - optional channel code in path if first segment matches a configured HTTP channel code
- Extended mapping/discovery/automap APIs:
  - mapping schemas now include channel fields + `input_key` while keeping `mqtt_topic` compatibility
  - new `GET /api/discovered-inputs`
  - existing `GET /api/discovered-topics` kept as legacy MQTT subset
  - `POST /api/mappings/automap` now channel-aware with ambiguity/conflict handling
- Updated frontend Input pane:
  - new `Input Channels` panel (add/edit/delete)
  - New/Edit mapping now uses channel selector + input key
  - discovery list switched to channel-aware discovered inputs with green "already configured" marker
- Updated proxy/routing:
  - nginx now proxies `/eos/input/` to backend
  - vite dev proxy now includes `/eos/input`
- Added docs:
  - `docs/archive/slice-6-input-channels.md`
  - README updated with slice-6 flow and API examples

### Slice-6 verification
- `python3 -m compileall -q backend/app backend/alembic/versions` passed.
- Frontend build was not executed on this VM because `node`/`npm` are currently not installed (`which node`/`which npm` empty).
- Validation update: `docker compose -f infra/docker-compose.yml build backend frontend` passed, including frontend `tsc -b && vite build` inside the containerized node toolchain.
- Post-merge migration hardening:
  - Fixed `20260220_0008` to be resilient across DB states by dropping `signal_measurements_raw` source-type constraint with `IF EXISTS` for both possible names (`ck_signal_measurements_raw_source_type` and `signal_measurements_raw_source_type_check`).
  - Added data normalization in migration for legacy fixed-value rows (`mqtt_topic=NULL`) before new source check re-creation.
- Runtime verification after migration hardening:
  - `docker compose -f infra/docker-compose.yml run --rm backend alembic upgrade head` passed.
  - `docker compose -f infra/docker-compose.yml up -d` started backend/frontend successfully.
  - `GET /status` includes new `input_channels` and `input_discovery` sections with sane values.
  - `GET /api/input-channels` returns seeded defaults (`mqtt-default`, `http-default`).
  - HTTP ingest verified:
    - `GET /eos/input/http-default/battery1_soc_factor=0.77` -> `accepted=true`, `mapping_matched=true` (with temporary test mapping).
    - `GET /eos/input/pv_power_kw?value=2.0` -> `accepted=true`, normalized key `eos/input/pv_power_kw`.
    - `POST /api/input/http/push` JSON ingest accepted with explicit timestamp.
  - Channel delete guard verified:
    - deleting channel with referenced mapping returns `409` and mapping count.
    - after deleting mapping, channel deletion returns `204`.
  - Discovery compatibility verified:
    - `GET /api/discovered-inputs?channel_type=mqtt&active_only=true` returns mqtt entries with `mapped_status`.
    - `GET /api/discovered-topics?active_only=true` still works as legacy mqtt subset.
- API response polish:
  - `DELETE /api/input-channels/{id}` conflict payload now uses `detail.message` (instead of nested `detail.detail`) for clearer client handling.
## 2026-02-20 (Input pane collapse state persistence)
- Added persistent collapse/expand behavior (localStorage-backed) for:
  - `Input Channels`
  - `Automap`
  - `Configured Mappings`
- Implemented panel toggle states in `frontend/src/App.tsx` with dedicated storage keys and restore-on-load defaults.
- Verified frontend build after change:
  - `docker compose -f infra/docker-compose.yml build frontend` passed (`tsc -b && vite build`).
- Redeployed frontend container:
  - `docker compose -f infra/docker-compose.yml up -d frontend`.
## 2026-02-20 (Parameters UX guard: battery id rename safety)
- Fixed a recurring apply failure source in Core Parameters:
  - when changing `Batterie.device_id`, all inverter references pointing to the old battery id are now auto-updated in the same core form state update.
- Added proactive UI validation visibility:
  - warning banner in Inverter section when `inverter.battery_id` references a non-existing battery id.
- Improved input ergonomics:
  - `Inverter -> Battery ID` now offers datalist suggestions from configured battery ids.
- Verified frontend build and redeploy:
  - `docker compose -f infra/docker-compose.yml build frontend`
  - `docker compose -f infra/docker-compose.yml up -d frontend`.
## 2026-02-20 (Unified details/triangle collapse UX + persisted parameter sections)
- Persisted collapse state added for Core parameter sections (`Standort`, `PV & Forecast`, `Speicher, EV, Inverter`, `Tarife & Last`, `Messwerte & EMR-Keys`) via localStorage key `eos-webapp:parameter-sections-open`.
- Inputs pane collapse UX switched from custom `Collapse/Expand` buttons to native `details/summary` triangle behavior for:
  - `Input Channels`
  - `New Mapping`
  - `Automap`
  - `Configured Mappings`
- Existing open-state persistence for these inputs panels remains active and now drives `details` open state.
- Added shared collapsible summary styling for panel sections in `frontend/src/styles.css`.
- Verified frontend build and container rollout:
  - `docker compose -f infra/docker-compose.yml build frontend`
  - `docker compose -f infra/docker-compose.yml up -d frontend`.
## 2026-02-20 (Slice 7: Inputs+Parameters unification + dynamic parameter ingest)
- Added migration `20260220_0009_parameter_dynamic_unification.py`:
  - `parameter_profile_revisions.source` includes `dynamic_input`
  - new tables: `parameter_bindings`, `parameter_input_events`
  - `signal_measurements_raw.source_type` includes `param_input`
- Added dynamic parameter backend services:
  - catalog service (`/api/parameters/dynamic-catalog`)
  - ingest service with shared parse/validation/revision-write/debounce-apply
  - setup checklist service (`/api/setup/checklist`)
- Added dynamic parameter APIs:
  - `GET/POST/PUT/DELETE /api/parameter-bindings`
  - `GET /api/parameter-bindings/events`
  - `GET /eos/param/{channel_or_path:path}`
  - `POST /api/input/param/push`
- Extended runtime/discovery/status:
  - MQTT ingest subscribes to enabled parameter-binding MQTT keys
  - `/api/discovered-inputs` supports `namespace=all|input|param` and `mapped_kind`
  - `/status` includes `parameters_dynamic` and `setup`
- Extended input-channel delete guard to include parameter-binding references (`409` if in use).
- Frontend updates:
  - left pane renamed to `Inputs & Setup`
  - tabs implemented: `Einrichtung`, `Live Inputs`, `Dynamische Parameter` (persisted)
  - dynamic parameter UI added: catalog, binding CRUD, event audit, param discovery
  - setup checklist card added
  - `Run-Center` headline applied in middle pane
  - dev/prod proxy routes include `/eos/param`
- Documentation:
  - new runbook `docs/archive/slice-7-inputs-parameters-unification.md`
  - README updated with slice-7 status, flow, and endpoint examples

### Slice-7 verification
- Backend compile check: `cd backend && python3 -m compileall app` passed.
- Frontend build check: `cd frontend && npm run build` passed.
- Notes:
  - Node/npm were installed on VM to run local frontend build.
  - `frontend/node_modules` and `frontend/package-lock.json` were created locally by `npm install` for build validation.
## 2026-02-21 (Slice 7 completion: Parameters moved left, middle run-center only)
- Frontend layout refactor completed:
  - Left pane (`Inputs & Setup`) now contains the full parameter profile/core/advanced/import-export editor under tab `Einrichtung`.
  - Middle pane is now a dedicated `Run-Center` (EOS runtime, EMS config, force run, run history) without parameter tabs.
  - Right pane (`Outputs`) unchanged functionally.
- Removed obsolete `parametersPaneTab` state and runtime tab toggle controls from middle pane.
- Verified end-to-end compile/build:
  - `cd backend && python3 -m compileall app` passed
  - `cd frontend && npm run build` passed
- Cleaned local frontend build artifacts from working tree (`dist`, `node_modules`, lock/tsbuildinfo) after verification.
## 2026-02-20 (Single-profile UX + central Settings import/export in Inputs & Setup)
- Removed profile switching/creation UI from `Einrichtung`; app now uses active profile internally and shows it read-only.
- Added central `Settings Import / Export (Inputs & Setup)` card with one package covering:
  - parameter draft payload
  - input channels
  - input mappings
  - dynamic parameter bindings
- Implemented unified setup export (`eos-webapp.setup.v1`) in frontend.
- Implemented unified setup import apply flow in frontend:
  - upsert channels by `code`
  - upsert mappings by `eos_field`
  - upsert parameter bindings by `(channel,input_key)`
  - optional apply-to-EOS after import
  - warnings shown for skipped/invalid/masked-secret cases
- Added backend support for channel export with optional secrets:
  - `GET /api/input-channels?include_secrets=true`
- Verification:
  - `docker compose -f infra/docker-compose.yml up -d --build backend frontend` passed
  - frontend bundle contains `Settings Import / Export (Inputs & Setup)` and `Run-Center`
  - backend startup and API calls stable after deploy
## 2026-02-20 (UI declutter pass: single-state settings UX)
- Simplified `Inputs & Setup` to reduce clutter:
  - moved central `Settings Import / Export (Inputs & Setup)` to top-level of left pane (above tabs)
  - removed masked/full export split; now one `Settings exportieren` action (full setup package)
  - removed profile-internal status line (`Draft/Applied`) from UI
- Simplified parameter save flow to one-state UX:
  - replaced multi-step actions (`Draft speichern`, `Strict validieren`, `Auf EOS anwenden`) with one action:
    `Änderungen speichern & auf EOS anwenden`
  - same simplification for Advanced JSON (`JSON speichern & auf EOS anwenden`)
- Removed old validation result panel from setup tab to keep the page compact.
- Import action simplified to a single `Settings importieren & anwenden` button.
- Backend/API compatibility kept (multi-profile backend still available, hidden in UI).
- Verified frontend bundle strings:
  - new labels present
  - old profile/draft/masked-export labels absent
- Final cleanup pass: removed remaining profile wording from setup error texts and unified export filename to `eos-inputs-setup.json`.
- Rebuilt frontend and redeployed container; verified bundle contains new Run-Center/settings labels and no old profile/masked-export labels.
## 2026-02-21 (Slice 8 HTTP-only unified Inputs & Setup)
- Backend
  - Added unified setup API router:
    - `GET /api/setup/fields`
    - `PATCH /api/setup/fields`
    - `GET /api/setup/readiness`
    - `GET /api/setup/export`
    - `POST /api/setup/import`
    - `POST /api/setup/set`
    - `GET /eos/set/{path}`
  - Added compatibility aliases:
    - `GET /eos/input/*` -> signal path mapping
    - `GET /eos/param/*` -> parameter path mapping
  - Added legacy `410 Gone` router for removed APIs:
    - `/api/input-channels*`, `/api/mappings/automap`, `/api/discovered-*`, `/api/parameter-bindings*`, `/api/setup/checklist`
  - Lifecycle/runtime switched to HTTP-only active path:
    - removed MQTT service startup
    - orchestrator MQTT publish path now safely no-ops/audits when MQTT disabled or unavailable
  - Added `SetupFieldEvent` model usage and migration `20260221_0010_http_only_unified_setup.py` (table + hard reset + single-state normalization).
  - Improved setup field logic:
    - required live fields become invalid/stale via configured threshold
    - export now includes all setup live signals
    - HTTP override activity uses configurable `HTTP_OVERRIDE_ACTIVE_SECONDS`.
- Frontend
  - Replaced previous tabbed/complex left pane with one unified `Inputs & Setup` pane.
  - Removed channel/mapping/automap/binding UI from active flow.
  - Added autosave behavior (debounce 1500ms + blur flush) for all editable setup fields.
  - Added required/optional/live sections with red mandatory highlighting until saved+valid.
  - Added per-field HTTP path template and HTTP active/inactive indicator.
  - Added central top-level `Settings Import / Export (Inputs & Setup)` card.
  - Kept middle `Run-Center` and right `Outputs` focused on runtime/run history/plan/solution.
- Infra/docs
  - Added `/eos/set` proxy in `frontend/vite.config.ts` and `frontend/nginx.conf`.
  - Added new runbook `docs/slice-8-http-only-unified-setup.md`.
  - Updated README with Slice-8 API flow and HTTP examples.
  - Updated `.env.example` defaults (`EOS_OUTPUT_MQTT_ENABLED=false`, `PARAM_DYNAMIC_ALLOW_MQTT=false`, added `HTTP_OVERRIDE_ACTIVE_SECONDS`).
- Follow-up fix:
  - Added conditional required logic in unified setup fields:
    - `feed_in_tariff_kwh` required only when `feedintariff.provider == FeedInTariffFixed`
    - `loadakkudoktor_year_energy_kwh` required only for `LoadAkkudoktor*`
  - Added resilient profile fallback in `SetupFieldService` to continue writes even if no profile is marked active.
  - Verified `GET /api/setup/readiness` now reaches `ready` when required data is satisfied.
- Follow-up compatibility alias:
  - added `/eos/input/battery_power_charge_kw` and `/eos/input/battery_power_charge_w` mapping to canonical `battery_power_w` in unified HTTP signal parser.
## 2026-02-21 (Slice 8 polish)
- Added explicit `410 Gone` handling for legacy mapping endpoints:
  - `GET/POST /api/mappings`
  - `PUT/DELETE /api/mappings/{id}`
  - `GET /api/live-values`
- Cleaned `README.md` to a Slice-8-only guide (removed outdated MQTT/automap/channel/profile setup flows).
- Re-verified runtime endpoints:
  - `GET /api/setup/fields` -> 200
  - `GET /api/setup/readiness` -> 200
  - `GET /eos/set/signal/pv_power_kw=2.0` -> 202
  - removed legacy endpoints return 410 as expected.
## 2026-02-20 (Run-Center diagnostics + force-run fallback hardening)
- Run analysis executed against live stack:
  - triggered force run (`run_id=93`) and inspected `/api/eos/runs/*`, EOS logs and direct EOS endpoints.
  - confirmed recurring `partial` runs are caused by EOS returning:
    - `GET /v1/energy-management/plan` -> `404` (`Did you configure automatic optimization?`)
    - `GET /v1/energy-management/optimization/solution` -> `404`.
- Backend improvements:
  - improved run error propagation in orchestrator:
    - `plan unavailable: <EOS detail>`
    - `solution unavailable: <EOS detail>`
    instead of generic `(404)` only.
  - added helper `_summarize_eos_error(...)` for concise EOS API detail extraction.
  - hardened force-run legacy fallback payload for modern `/optimize` schema:
    - wraps series under `ems`
    - provides required top-level keys `pv_akku`, `inverter`, `eauto`
    - derives `preis_euro_pro_wh_akku` from battery config
    - trims series to common length.
  - fallback result can now be persisted as a `solution` artifact when successful.
  - `/api/eos/runs/{id}/solution` now returns latest solution artifact regardless of artifact_key (supports fallback key variants).
- Findings from live fallback test:
  - legacy `/optimize` currently fails in EOS with `Optimize error: Unsupported fill method: linear.`
  - this error is now visible in run `error_text` for user troubleshooting.
- Run-Center UX (frontend) upgraded:
  - added run-type explanation (Auto vs Force, status semantics).
  - added run statistics chips (total/auto/force/running/success/partial/failed).
  - added run filters (source + status).
  - added selected-run analysis block with:
    - duration, EOS timestamp, artifact summary
    - user-readable hints derived from run/error context
    - raw error text for precise debugging.
## 2026-02-20 (Run pipeline stabilized + PV import guard)
- Root-cause analysis and runtime fix:
  - identified failing EOS runs due to `Unsupported fill method: linear` with `pvforecast_dc_power` being `null`.
  - confirmed and applied working PV import payload containing **both** numeric arrays:
    - `pvforecast_ac_power`
    - `pvforecast_dc_power`
- Validation hardening in backend (`EosSettingsValidationService`):
  - added domain validation for `PVForecastImport.import_json` (must be valid JSON object with numeric AC/DC arrays).
  - added normalization rule:
    - when `pvforecast_dc_power` is missing/invalid, copy from `pvforecast_ac_power` and persist normalized JSON.
  - added provider-specific guard for `PVForecastAkkudoktor`:
    - reject `surface_tilt <= 0` with clear error (upstream API rejects tilt=0).
- Run-Center UX refinement:
  - improved hint text for linear-fill failures with direct remediation.
  - added pipeline step visualization for selected run:
    - health -> prediction keys -> prediction series -> plan -> solution.
- Verification:
  - rebuild + restart backend/frontend containers.
  - force runs completed successfully (`run_id=100`, `run_id=101`):
    - `status=success`
    - plan and solution artifacts present.
## 2026-02-20 (Final run validation)
- Executed additional force-run validations against current stack:
  - `run_id=102` -> `status=success` (plan + solution present, no EOS errors since run start).
  - `run_id=103` -> `status=success` (plan + solution present).
- Confirmed active EOS runtime configuration:
  - `pvforecast.provider=PVForecastImport`
  - `PVForecastImport.import_json` contains both `pvforecast_ac_power` and `pvforecast_dc_power`.
  - `ems.mode=OPTIMIZATION`, `ems.interval=900`.
## 2026-02-20 (Slice 9: HTTP output dispatch + plausibility + output UX)
- Backend data model + migration:
  - added Alembic migration `20260221_0011_http_output_dispatch.py`.
  - `eos_plan_instructions` extended with `execution_time`.
  - new tables:
    - `output_targets`
    - `output_dispatch_events`.
- Backend runtime:
  - implemented `OutputDispatchService` with:
    - scheduled dispatch,
    - heartbeat dispatch,
    - force dispatch endpoint integration,
    - idempotency keys,
    - retry chain audit,
    - no-grid-charge hard block.
  - wired service into app lifespan and `/status` (`output_dispatch` section + config values).
- Backend API:
  - added endpoints:
    - `GET /api/eos/outputs/current`
    - `GET /api/eos/outputs/timeline`
    - `GET /api/eos/outputs/events`
    - `POST /api/eos/outputs/dispatch/force`
    - `GET /api/eos/runs/{id}/plausibility`
    - `GET/POST/PUT /api/eos/output-targets`.
  - plan response now includes `execution_time` per instruction.
- Frontend outputs pane refactor:
  - added cards for:
    - active decisions,
    - next state changes,
    - dispatch log,
    - output targets (CRUD + enable/disable),
    - plausibility findings.
  - kept `Plan (JSON)` and `Solution (JSON)` as debug panel.
- Ops/dependency updates:
  - added `pandas==2.2.3` to backend requirements.
  - added `scripts/auto-install.sh` for host dependency install + compose startup + migration + health checks.
  - updated `README.md` and `docs/vm-setup.md` to script-first workflow.
  - added `docs/slice-9-http-output-dispatch.md`.
- Verification (live stack):
  - ran migration to head.
  - triggered force run `run_id=105` -> `status=success`.
  - verified `execution_time` is persisted and served (`/api/eos/runs/105/plan`).
  - created HTTP output target for `battery1` and forced dispatch.
  - observed dispatch audit event `status=sent`, `http_status=200`.
  - verified new output endpoints return expected data.

## 2026-02-20 (Stabilisierung: HTTP-Only + EMR + Run/Dispatch)
- Fixed HTTP signal ingest -> EMR dual-write gap:
  - `SetupFieldService` now forwards live signals into `EmrPipelineService`.
  - Added `EmrPipelineService.process_signal_value(...)` for direct HTTP signal processing.
  - Result: `power_samples` and `energy_emr` now update continuously from `/eos/set/signal/*`.
- Fixed setup field reject semantics:
  - rejected parameter updates are no longer persisted into draft state.
  - invalid updates now keep last valid value (prevents hidden apply drift).
- Fixed stale error UX on recovered fields:
  - field error now only surfaces when field is currently invalid/missing.
- Improved force dispatch API response:
  - `POST /api/eos/outputs/dispatch/force` now returns actual queued active resources.
- Status payload consistency:
  - `/status` now includes `service: backend` again.
- Live verification on running stack:
  - force run `113` completed `success` with plan+solution artifacts.
  - output current/timeline/events endpoints return consistent instruction data.
  - dispatch events show expected `sent|blocked|skipped_no_target` behavior.
- Follow-up fixes after live validation:
  - setup reject-flow now preserves previous valid parameter payload (invalid attempts no longer overwrite current state).
  - `/status` includes `service=backend` again for monitor consistency.
  - output force-dispatch response now lists actual active queued resources.
  - measurement-sync status semantics adjusted: missing optional keys are skipped with warnings but run stays `ok` unless a real push fails.
- Run-Center erweitert um echte Prediction-Refresh-Runtypen (pv/prices/all/load):
  - neuer Backend-Endpoint `POST /api/eos/runs/predictions/refresh`.
  - Orchestrator kann nun manuelle Prediction-Runs asynchron anlegen, auditieren und persistieren.
  - Prediction-Runs speichern `prediction_refresh`-Artefakt plus `prediction_keys`/`prediction_series`.
- EOS-Client erweitert um provider-spezifische Prediction-Updates und Provider-Liste.
- Output-Dispatch gegen neue Runtypen gehärtet:
  - Default-Run-Auswahl nutzt jetzt den letzten erfolgreichen Run **mit Plan-Instruktionen**, damit Prediction-only-Runs Dispatch nicht verdrängen.
- Frontend Run-Center erweitert:
  - Buttons `PV Forecast Refresh`, `Preis Refresh`, `Prediction All Refresh`.
  - Run-Statistik/Filter um Quelle `prediction` ergänzt.
  - Pipeline-Hinweise für Prediction-only-Runs in Anwendersprache ergänzt.
- Live-Verifikation:
  - Prediction-Runs wurden erzeugt und korrekt als `prediction_refresh_*` in Historie protokolliert.
  - Bei upstream-Fehlern (PVForecastAkkudoktor 400) laufen Prediction-Runs als `partial` weiter und erfassen trotzdem Prediction-Artefakte.
  - Force-Run (`122`) weiterhin erfolgreich; Outputs/Dispatch bleiben auf letztem planbaren Run stabil.
- Refinements for prediction refresh runs:
  - provider refresh errors are summarized to concise first-line messages (no traceback flooding in UI).
  - all-scope refresh degrades to `partial` with clear reason instead of hard-failing the whole run history flow.
  - observed EOS behavior: provider-specific `/v1/prediction/update/{provider}` still fails if PVForecast provider update fails upstream; surfaced as partial with actionable reason.
- Validation pass:
  - prediction refresh run `126` -> `partial` (expected due upstream PV provider 400), artifacts persisted.
  - force optimization run `127` -> `success`, plan+solution captured.
  - output current remains pinned to latest dispatchable optimization run (not prediction-only runs).

## 2026-02-20 (Force-Run chaining + measurement ingestion hardening)
- Force-run preflight improvements:
  - added optional pre-refresh before force-run (`EOS_FORCE_RUN_PRE_REFRESH_ENABLED`, `EOS_FORCE_RUN_PRE_REFRESH_SCOPE`).
  - force-run now writes preflight artifacts:
    - `prediction_refresh` with key `pre_force_<scope>`
    - `measurement_push` with key `pre_force_latest`
  - added previous successful run metadata to run-context snapshots (`assembled_eos_input_json.previous_successful_run`).
- Measurement sync hardening:
  - switched availability checks from `measurement.keys` config object to EOS registry endpoint `/v1/measurement/keys`.
  - unavailable keys are now explicitly skipped with reason `key not available in EOS measurement registry`.
  - EMR pushes remain stable (`grid_*_emr_kwh`, `house_load_emr_kwh`, `pv_production_emr_kwh`).
- Live verification:
  - force run `142` completed with plan+solution and preflight artifacts persisted.
  - run context `142` includes `previous_successful_run` metadata (run `141`).
  - `power_samples` and `energy_emr` continue to update from HTTP ingest; EMR monotonicity check showed zero negative steps.

## 2026-02-20 (PVForecastAkkudoktor refresh recovery)
- Problem confirmed live:
  - direct EOS prediction refresh (`/v1/prediction/update*`) fails with HTTP 400 when `pvforecast.provider=PVForecastAkkudoktor`.
  - error points to upstream akkudoktor forecast request with invalid azimuth handling (`.../forecast?...azimuth=0...`).
- Backend hotfix in `app/services/eos_orchestrator.py`:
  - added automatic PV fallback during prediction refresh failure handling.
  - when refresh fails with a PV/Akkudoktor signature and fallback is enabled, orchestrator switches EOS config path `pvforecast/provider` to `PVForecastImport`, persists config file, and retries refresh.
  - refresh artifact now records `fallback_applied[]` for full traceability.
- New settings:
  - `EOS_PREDICTION_PV_IMPORT_FALLBACK_ENABLED=true`
  - `EOS_PREDICTION_PV_IMPORT_PROVIDER=PVForecastImport`
  - propagated to `.env.example` and compose env pass-through.
- Verification:
  - run `144` (`force_run`) finished `success` with plan+solution artifacts.
  - run `145` was started after forcing provider back to `PVForecastAkkudoktor`; finished `success` and stored `prediction_refresh.fallback_applied.applied=true`.
  - direct EOS provider refresh call still reproduces upstream 400, confirming fallback is required for robustness until upstream fix.
- Additional PV provider fix (beyond fallback):
  - Implemented Akkudoktor south-azimuth compatibility workaround in `ParameterProfileService` apply path.
  - User/profile stays at intuitive `surface_azimuth=180` (south), but EOS apply transforms to `360` for `PVForecastAkkudoktor` to avoid upstream `azimuth=0` rejection.
  - Bootstrap normalization maps EOS `360` back to profile/UI `180` to keep UX consistent.
- Live verification after patch:
  - `GET /api/setup/fields` still shows `param.pvforecast.planes.0.surface_azimuth = 180`.
  - EOS config after apply shows `surface_azimuth=360`.
  - Direct EOS provider refresh `/v1/prediction/update/PVForecastAkkudoktor` returns `200`.
  - Force run `147` completed `success` with `prediction_refresh.fallback_applied=[]` (no fallback required).
- UI enhancement:
  - Added inline info box on `param.pvforecast.planes.0.surface_azimuth` when provider is `PVForecastAkkudoktor`.
  - Message explains active compatibility mapping.
- Workaround refinement:
  - Replaced coarse mapping `180 -> 360` with near-value mapping `180 -> 179.9` for `PVForecastAkkudoktor`.
  - Goal: keep south-facing semantics while avoiding the exact failing edge value that EOS forwards as upstream `azimuth=0`.
  - Setup/UI still displays `180`, EOS receives `179.9` on apply.
- Validation:
  - direct `POST /v1/prediction/update/PVForecastAkkudoktor` returns `200` after apply.
  - force run `148` completed `success` and `prediction_refresh.fallback_applied=[]`.
- Market-price switch applied (live config/profile via setup HTTP endpoints):
  - `elecprice.provider=ElecPriceEnergyCharts`
  - `elecprice.energycharts.bidding_zone=DE-LU`
  - `feedintariff.provider=FeedInTariffFixed`
  - `feed_in_tariff_kwh=0.09`
- Verification:
  - `elecprice_marketprice_wh` now varies across horizon (48 points, 45 distinct values) -> fetched market series, not constant import.
  - prediction refresh for `ElecPriceEnergyCharts` returns `200`.
  - force run `149` completed `success` with updated pricing inputs.

## 2026-02-20 (Fixed import field + direct-marketing feed-in sync with fixed import)
- Setup field gap fixed:
  - Added explicit field `param.elecprice.elecpriceimport.import_json.value` to unified `Inputs & Setup`.
  - Label: `Netzbezugspreis fix` (unit `EUR/kWh`), including HTTP path hint `/eos/set/param/elecprice/elecpriceimport/import_json/value=<value>`.
  - Required logic is provider-aware: required only when `elecprice.provider=ElecPriceImport`.
- EOS schema compatibility fix for that field:
  - UI/API value is handled as user-friendly `EUR/kWh`.
  - Backend now serializes to EOS-compatible `elecprice.elecpriceimport.import_json` JSON-string series (`elecprice_marketprice_wh`) for prediction horizon.
  - Existing EOS import-json strings are parsed back to `EUR/kWh` for display.
- Price refresh behavior adjusted for your rule:
  - Rule: import price fixed (e.g. `0.23 EUR/kWh`) + feed-in from spot.
  - Feed-in mirror now loads spot data even when `elecprice.provider=ElecPriceImport` by temporarily switching EOS price provider to `ElecPriceEnergyCharts`, fetching market series, then restoring `ElecPriceImport`.
  - Resulting run artifact contains `feedin_spot_sync.source_context.temporary_provider_switch=true`.
- Live verification:
  - `/api/setup/fields` shows the new fixed import field with current value `0.23`.
  - `PATCH /api/setup/fields` with `elecprice.provider=ElecPriceImport` and `...import_json.value=0.23` saves and applies successfully.
  - Prediction refresh run `152` succeeded; artifact reports `feedin_spot_sync.points=48`, `unique_values=48`.
  - EOS config remains `elecprice.provider=ElecPriceImport`, while `feed_in_tariff_wh` prediction series is variable (spot).

## 2026-02-20 (Automatic run cadence tuning)
- Confirmed current automatic cadence settings in EOS config:
  - `ems.mode=OPTIMIZATION`
  - `ems.interval=900` (15 minutes)
- Updated and persisted startup delay:
  - `ems.startup_delay` changed from `5` to `1` second.
- Note:
  - Interval mode guarantees 15-minute spacing, but does not hard-guarantee wall-clock alignment to exact `:00/:15/:30/:45`.

## 2026-02-20 (Aligned quarter-hour scheduler in webapp)
- Added webapp-owned aligned scheduler in `EosOrchestratorService`:
  - new envs:
    - `EOS_ALIGNED_SCHEDULER_ENABLED=true`
    - `EOS_ALIGNED_SCHEDULER_MINUTES=0,15,30,45`
    - `EOS_ALIGNED_SCHEDULER_DELAY_SECONDS=1`
    - `EOS_ALIGNED_SCHEDULER_BASE_INTERVAL_SECONDS=86400`
  - scheduler runs in dedicated thread and queues force-like runs at exact aligned slots (`:00/:15/:30/:45 + 1s`).
  - scheduled runs are stored as:
    - `trigger_source=automatic`
    - `run_mode=aligned_schedule`
- Runtime/status visibility:
  - `/api/eos/runtime` collector now includes:
    - `aligned_scheduler_enabled`
    - `aligned_scheduler_minutes`
    - `aligned_scheduler_delay_seconds`
    - `aligned_scheduler_next_due_ts`
    - `aligned_scheduler_last_trigger_ts`
    - `aligned_scheduler_last_skip_reason`
  - Run-Center UI (EOS Runtime card) shows aligned scheduler state/next slot/last trigger/last skip.
- Validation:
  - temporary slot test (`minute=04`, `delay=1`) triggered run `155` at `23:04:01Z`.
  - run `155` finished `success` with `trigger_source=automatic`, `run_mode=aligned_schedule`.
  - switched backend back to default slots `0,15,30,45` after test.

## 2026-02-27 (UI rounding + docs consistency)
- Fixed setup number input rendering to avoid floating-point display artifacts.
- `kW` values now render with max 3 decimals in operator-facing setup UI.
- Added doc clarifications for the precision rule:
  - `README.md` (`UI Unit Contract`)
  - `docs/ui-unit-policy.md` (`Display precision`)
  - `AGENTS.md` (`UI Unit Policy`)
- Verification:
  - `cd frontend && npm run build` passed.
