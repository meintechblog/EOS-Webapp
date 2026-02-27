# EOS Price Prediction Quality

## Purpose

This note documents practical levers to improve electricity-price prediction quality in EOS and how EOS-Webapp uses them.

## What Matters Most

1. Use a real market-data provider.
   - Prefer `ElecPriceEnergyCharts` or `ElecPriceAkkudoktor`.
   - `ElecPriceImport` with a constant series will always produce a flat price line.
2. Set enough forecast/history horizon.
   - `prediction.hours` controls future horizon.
   - `prediction.historic_hours` controls how much history is retained for model fitting.
   - In this Webapp setup, `prediction.historic_hours` is intentionally set to `672h` (4 weeks).
   - EOS then uses as much effective history as available from retained provider/DB data.
3. Keep provider settings correct.
   - For EnergyCharts, ensure correct `elecprice.energycharts.bidding_zone`.
   - `elecprice.charges_kwh` and `elecprice.vat_rate` are applied on top of market prices.
4. Refresh predictions before critical force runs.
   - Run `Prediction Refresh (prices/all)` shortly before `Force Run` when data might be stale.

## EOS Internals Relevant for Quality

- EnergyCharts provider fetches a broad history window (up to ~35 days) and then extrapolates missing horizon.
- Extrapolation uses ETS seasonality when enough datapoints are available:
  - `> 800` datasets: weekly seasonality (168h)
  - `> 168` datasets: daily seasonality (24h)
  - otherwise median fallback
- `prediction.historic_hours` has no strict EOS max in config (`ge=0`); this Webapp intentionally limits the setting to 4 weeks for stable runtime/quality tradeoff.

## EOS-Webapp Behavior

- Run-Center now exposes a prominent horizon dropdown.
- On apply, Webapp updates:
  - `prediction.hours`
  - `prediction.historic_hours` (`672h` / 4 weeks target)
  - and, when available in payload, `optimization.horizon_hours` or legacy `optimization.hours`
- Run-Historie shows live runtime for running jobs and final runtime for finished jobs.
- Run-Historie entries include per-run prediction metrics:
  - configured target horizon (from run context snapshot),
  - effective solution horizon (derived from prediction timestamps),
  - configured historic horizon, point count, and price range (when available).

## Automatic 4-Week Price Backfill (Restart-Backfill)

- EOS-Webapp can auto-check whether raw electricity-price history is long enough for quality fitting.
- Check basis is strictly raw `prediction/series` data for `elecprice_marketprice_wh` in the last 4 weeks.
- If raw coverage is too short (`< EOS_PRICE_BACKFILL_MIN_HISTORY_HOURS`), Webapp can:
  1. trigger EOS restart (`/v1/admin/server/restart`),
  2. wait for EOS recovery,
  3. trigger a forced refresh for the active price provider,
  4. wait up to `EOS_PRICE_BACKFILL_SETTLE_SECONDS` for provider backfill materialization,
  5. re-check raw coverage.
- Cooldown prevents restart loops (`EOS_PRICE_BACKFILL_COOLDOWN_SECONDS`, default 24h).
- This flow does **not** switch price providers automatically.
- EOS calls in this path use transient retry logic plus request timeout `EOS_HTTP_TIMEOUT_SECONDS` to reduce false partials after restart.

Important:

- `prediction/list` can appear "full" because EOS resamples/interpolates.
- Therefore list completeness is diagnostics only; quality checks use raw `prediction/series`.

## Storage Strategy (HTTP-only hard cleanup)

- Full prediction payloads stay in `eos_artifacts` per run (`prediction_series` artifacts).
- Signal backbone persistence for `prediction.*` is intentionally reduced to chart-relevant keys:
  - `prediction.elecprice_marketprice_wh`
  - `prediction.elecprice_marketprice_kwh`
  - `prediction.pvforecast_ac_power`
  - `prediction.pvforecastakkudoktor_ac_power_any`
  - `prediction.loadforecast_power_w`
  - `prediction.load_mean_adjusted`
  - `prediction.load_mean`
  - `prediction.loadakkudoktor_mean_power_w`
- This keeps chart quality while reducing raw DB growth.

Retention defaults used by this setup:

- raw: `14d`
- rollup 5m: `180d`
- rollup 1h: `1095d`

## Sources

- EOS docs (Prediction): `https://docs.akkudoktor.net/akkudoktoreos/prediction.html`
- EOS docs (Automatic Optimization): `https://docs.akkudoktor.net/akkudoktoreos/automatic_optimization.html`
- EOS source (EnergyCharts provider): `https://github.com/Akkudoktor-EOS/EOS/blob/main/src/akkudoktoreos/prediction/elecpriceenergycharts.py`
- EOS source (Akkudoktor provider): `https://github.com/Akkudoktor-EOS/EOS/blob/main/src/akkudoktoreos/prediction/elecpriceakkudoktor.py`
