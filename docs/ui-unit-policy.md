# UI Unit Policy (Operator View)

## Goal

Operator-facing values in EOS Webapp must be shown in one consistent unit set:

- Prices: `ct/kWh`
- Capacity: `kWh`
- Power: `kW`

This applies to setup fields, setup HTTP templates, and setup export signal values.

## Internal storage vs UI display

The backend may keep internal EOS-compatible units:

- Power in `W`
- Capacity in `Wh`
- Prices in `EUR/kWh` (and EOS-internal series in `EUR/Wh`)

But all conversions must happen transparently so operators only see `ct/kWh`, `kWh`, `kW`.

## Backend implementation points

Primary file:

- `backend/app/services/setup_fields.py`

Key parts:

- `_UI_TO_STORAGE_FACTORS`: converts UI input -> internal storage.
- `_build_field_state(...)`: converts internal value -> UI value.
- `_param_path_to_field_id(...)`: maps `/eos/set/param/...` path aliases and scales legacy paths.
- `_resolve_signal_field_id_and_input_scale(...)`: accepts `*_kw` and legacy `*_w`.
- `_signal_export_key_from_internal(...)`: exports setup signal keys as `*_kw` instead of `*_w`.

## Path contract

Preferred UI-facing paths use explicit UI units:

- `.../capacity_kwh`
- `.../min_charge_power_kw`
- `.../max_charge_power_kw`
- `.../max_power_kw`
- `.../value_ct_per_kwh`
- `.../charges_ct_per_kwh`
- `.../feed_in_tariff_ct_per_kwh`
- `.../signal/*_kw`

Legacy aliases are still accepted for compatibility (for example `*_w`, `capacity_wh`, `charges_kwh`, `feed_in_tariff_kwh`) but are converted internally and should not be shown in UI templates.

## Mandatory verification

Run after every unit-related change:

```bash
./scripts/check-ui-unit-policy.sh
```

The script checks:

- setup field units for power/capacity/price
- no forbidden display units (`W`, `Wh`, `EUR/kWh`, `EUR/Wh`) in setup field units
- setup HTTP templates do not expose legacy unit paths
- setup export signal keys do not expose `_w`/`_wh`

## CI enforcement

GitHub workflow:

- `.github/workflows/ui-unit-policy.yml`

The workflow starts backend + database, runs migrations, and executes `./scripts/check-ui-unit-policy.sh` on every push and pull request.
