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
   - For EOS price models, `>= 840h` is a practical quality threshold (weekly ETS path needs `> 800` records).
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
- `prediction.historic_hours` has no strict EOS max in config (`ge=0`); practical gains above ~840h depend on available retained provider records and runtime cost.

## EOS-Webapp Behavior

- Run-Center now exposes a prominent horizon dropdown.
- On apply, Webapp updates:
  - `prediction.hours`
  - `prediction.historic_hours` (adaptive: minimum `840h`, currently capped at `2160h`)
  - and, when available in payload, `optimization.horizon_hours` or legacy `optimization.hours`
- Run-Historie shows live runtime for running jobs and final runtime for finished jobs.
- Run-Historie entries include per-run prediction metrics:
  - configured target horizon (from run context snapshot),
  - effective solution horizon (derived from prediction timestamps),
  - configured historic horizon, point count, and price range (when available).

## Sources

- EOS docs (Prediction): `https://docs.akkudoktor.net/akkudoktoreos/prediction.html`
- EOS docs (Automatic Optimization): `https://docs.akkudoktor.net/akkudoktoreos/automatic_optimization.html`
- EOS source (EnergyCharts provider): `https://github.com/Akkudoktor-EOS/EOS/blob/main/src/akkudoktoreos/prediction/elecpriceenergycharts.py`
- EOS source (Akkudoktor provider): `https://github.com/Akkudoktor-EOS/EOS/blob/main/src/akkudoktoreos/prediction/elecpriceakkudoktor.py`
