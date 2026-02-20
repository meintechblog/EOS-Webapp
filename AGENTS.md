# AGENTS.md

## EOS Webapp: UI Unit Policy (mandatory)

For every user-facing value in the web UI, keep this policy:

- Prices: `ct/kWh`
- Capacity: `kWh`
- Power: `kW`

Do not show `W`, `Wh`, `EUR/kWh`, or `EUR/Wh` to operators in the UI.

## Internal compatibility

Internal storage may still use legacy units (for EOS/API compatibility), but conversion must be handled in backend/frontend mapping layers.

- UI input/output: `ct/kWh`, `kWh`, `kW`
- Internal storage (allowed): `EUR/kWh`, `Wh`, `W`

## When adding or changing setup fields

If you touch `backend/app/services/setup_fields.py`:

1. Ensure field `unit` follows UI policy.
2. Ensure `http_path_template` exposes UI units (`*_kw`, `*_kwh`, `*_ct_per_kwh`).
3. If internal storage unit differs, update `_UI_TO_STORAGE_FACTORS`.
4. Keep legacy `/eos/set` aliases only for backward compatibility.

## Required verification before commit

Run:

```bash
./scripts/check-ui-unit-policy.sh
```

This is the minimum gate for unit consistency in setup fields and exported signal keys.
