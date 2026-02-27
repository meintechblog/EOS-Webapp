#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://127.0.0.1:8080}}"

if ! command -v curl >/dev/null 2>&1; then
  echo "[FAIL] curl is required" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "[FAIL] jq is required" >&2
  exit 1
fi

FIELDS_JSON="$(curl -fsS "${BASE_URL}/api/setup/fields")"
EXPORT_JSON="$(curl -fsS "${BASE_URL}/api/setup/export")"

fail=0

check_fields() {
  local expr="$1"
  local description="$2"
  if jq -e "${expr}" >/dev/null <<<"${FIELDS_JSON}"; then
    echo "[OK] ${description}"
  else
    echo "[FAIL] ${description}" >&2
    fail=1
  fi
}

check_export() {
  local expr="$1"
  local description="$2"
  if jq -e "${expr}" >/dev/null <<<"${EXPORT_JSON}"; then
    echo "[OK] ${description}"
  else
    echo "[FAIL] ${description}" >&2
    fail=1
  fi
}

check_fields '
  def idx: map({key: .field_id, value: .}) | from_entries;
  (idx["param.elecprice.elecpriceimport.import_json.value"] // {} | .unit) == "ct/kWh"
' "fixed import price is shown as ct/kWh"

check_fields '
  def idx: map({key: .field_id, value: .}) | from_entries;
  (idx["param.feedintariff.provider_settings.FeedInTariffFixed.feed_in_tariff_kwh"] // {} | .unit) == "ct/kWh"
' "feed-in tariff is shown as ct/kWh"

check_fields '
  def idx: map({key: .field_id, value: .}) | from_entries;
  (idx["param.elecprice.charges_kwh"] // {} | .unit) == "ct/kWh"
' "price surcharge is shown as ct/kWh"

check_fields '
  [
    .[]
    | select(
        (.field_id | test("^param\\.devices\\.batteries\\.[0-9]+\\.capacity_wh$"))
        or (.field_id | test("^param\\.devices\\.electric_vehicles\\.[0-9]+\\.capacity_wh$"))
        or (.field_id | test("^param\\.devices\\.home_appliances\\.[0-9]+\\.consumption_wh$"))
      )
    | .unit == "kWh"
  ]
  | all
' "all capacity/consumption fields are shown as kWh"

check_fields '
  [
    .[]
    | select(
        (.field_id | test("^param\\.pvforecast\\.planes\\.[0-9]+\\.(peakpower|inverter_paco)$"))
        or (.field_id | test("^param\\.devices\\.batteries\\.[0-9]+\\.(min_charge_power_w|max_charge_power_w)$"))
        or (.field_id | test("^param\\.devices\\.inverters\\.[0-9]+\\.max_power_w$"))
        or (.field_id | test("^param\\.devices\\.electric_vehicles\\.[0-9]+\\.(min_charge_power_w|max_charge_power_w)$"))
        or (.field_id | test("^signal\\.(house_load_w|pv_power_w|grid_import_w|grid_export_w|battery_power_w)$"))
      )
    | .unit == "kW"
  ]
  | all
' "all power fields are shown as kW"

check_fields '
  [.[].unit // "" | ascii_downcase | select(. == "w" or . == "wh" or . == "eur/kwh" or . == "eur/wh")]
  | length == 0
' "no forbidden display units (W, Wh, EUR/kWh, EUR/Wh) appear in setup field units"

check_fields '
  [.[].http_path_template // "" | ascii_downcase | select(test("_w(=|/|$)|_wh(=|/|$)|eur/kwh|eur/wh|euro_pro_wh"))]
  | length == 0
' "setup HTTP templates use UI unit naming"

check_export '
  ((.payload // {}) | .signal_values // {} | keys)
  | map(ascii_downcase)
  | map(select(test("_w$|_wh$")))
  | length == 0
' "setup export signal keys do not expose _w/_wh suffixes"

if [[ "${fail}" -ne 0 ]]; then
  echo "[RESULT] UI unit policy check failed" >&2
  exit 1
fi

echo "[RESULT] UI unit policy check passed"
