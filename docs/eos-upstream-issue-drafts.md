# EOS Upstream Issue Drafts (DE/EN)

Purpose: Copy/paste-ready GitHub issue drafts based on `docs/eos-upstream-findings.md`.

Target repo:

- `https://github.com/Akkudoktor-EOS/EOS`

Notes:

- Keep one issue per finding.
- Attach fresh artifacts when opening (raw API response, config snapshot, timestamps).
- Drafts below are concise and technical.

## EOS-UPSTREAM-001

### DE - Titel

`solution.prediction.data` ist deutlich kuerzer als konfigurierter Horizon (96h)

### DE - Body (Markdown)

## Zusammenfassung

Bei einem Lauf mit `prediction.hours=96` und `optimization.horizon_hours=96` enthaelt `solution.prediction.data` nur 47 Punkte, waehrend die gespeicherte Prediction-Serie fuer denselben Lauf 96 Punkte enthaelt.

## Beobachtung

- Konfigurierter Zielhorizont: 96h
- `solution.prediction.data` Laenge: 47
- `prediction-series` (normalisierte Punkte) Laenge: 96

## Erwartetes Verhalten

Die Solution-Prediction sollte konsistent zum konfigurierten Horizon bzw. zur gespeicherten Prediction-Serie sein (oder klar dokumentiert abweichen).

## Reproduktion

```bash
curl -s http://127.0.0.1:8080/api/eos/runs/177/context | jq '{prediction_hours:(.runtime_config_snapshot_json.prediction.hours), optimization_horizon_hours:(.runtime_config_snapshot_json.optimization.horizon_hours)}'
curl -s http://127.0.0.1:8080/api/eos/runs/177/solution | jq '.payload_json.prediction.data | length'
curl -s http://127.0.0.1:8080/api/eos/runs/177/prediction-series | jq '.points | length'
```

## Impact

UI/Downstream kann einen zu kurzen Vorschauhorizont anzeigen oder falsch interpretieren.

## Zusatzinfo

Beobachtet am 2026-02-21, Run `#177`.

### EN - Title

`solution.prediction.data` is significantly shorter than configured horizon (96h)

### EN - Body (Markdown)

## Summary

For a run configured with `prediction.hours=96` and `optimization.horizon_hours=96`, `solution.prediction.data` contains only 47 points, while the stored prediction series for the same run contains 96 points.

## Observed behavior

- Configured target horizon: 96h
- `solution.prediction.data` length: 47
- normalized `prediction-series` length: 96

## Expected behavior

Solution prediction length should be consistent with configured horizon and/or stored prediction series (or clearly documented if intentionally different).

## Reproduction

```bash
curl -s http://127.0.0.1:8080/api/eos/runs/177/context | jq '{prediction_hours:(.runtime_config_snapshot_json.prediction.hours), optimization_horizon_hours:(.runtime_config_snapshot_json.optimization.horizon_hours)}'
curl -s http://127.0.0.1:8080/api/eos/runs/177/solution | jq '.payload_json.prediction.data | length'
curl -s http://127.0.0.1:8080/api/eos/runs/177/prediction-series | jq '.points | length'
```

## Impact

UI/downstream consumers may show or assume a shorter optimization horizon than configured.

## Context

Observed on 2026-02-21, run `#177`.

## EOS-UPSTREAM-002

### DE - Titel

`PredictionContainer.key_to_dataframe`: `end_datetime` wird bei `provider.max_datetime` falsch auf `provider.min_datetime` gesetzt

### DE - Body (Markdown)

## Zusammenfassung

In `src/akkudoktoreos/core/dataabc.py` scheint in `PredictionContainer.key_to_dataframe(...)` eine inkonsistente Zuweisung vorzuliegen.

## Code-Stelle (aktuelles `main`)

```python
elif (
    provider.max_datetime
    and compare_datetimes(provider.max_datetime, end_datetime).gt
):
    end_datetime = provider.min_datetime
```

## Problem

Semantisch wird hier `provider.max_datetime` geprueft, aber `provider.min_datetime` zugewiesen.

## Erwartetes Verhalten

Wahrscheinlich sollte hier `end_datetime = provider.max_datetime` stehen.

## Vermuteter Impact

Falsches Clipping des Endzeitpunkts, potenziell verkuerzter oder inkonsistenter Datenbereich.

## Referenz

https://raw.githubusercontent.com/Akkudoktor-EOS/EOS/main/src/akkudoktoreos/core/dataabc.py

### EN - Title

`PredictionContainer.key_to_dataframe`: `end_datetime` is set to `provider.min_datetime` when checking `provider.max_datetime`

### EN - Body (Markdown)

## Summary

In `src/akkudoktoreos/core/dataabc.py`, `PredictionContainer.key_to_dataframe(...)` appears to contain an inconsistent assignment.

## Code (current `main`)

```python
elif (
    provider.max_datetime
    and compare_datetimes(provider.max_datetime, end_datetime).gt
):
    end_datetime = provider.min_datetime
```

## Problem

The condition checks `provider.max_datetime`, but assigns `provider.min_datetime`.

## Expected behavior

This likely should be `end_datetime = provider.max_datetime`.

## Potential impact

Incorrect end-time clipping, potentially shortening or distorting selected data range.

## Reference

https://raw.githubusercontent.com/Akkudoktor-EOS/EOS/main/src/akkudoktoreos/core/dataabc.py

## EOS-UPSTREAM-003

### DE - Titel

`PredictionContainer.key_to_dataframe`: Ergebnis von `end_datetime.add(seconds=1)` wird nicht verwendet

### DE - Body (Markdown)

## Zusammenfassung

In `src/akkudoktoreos/core/dataabc.py` wird `end_datetime.add(seconds=1)` aufgerufen, das Ergebnis aber nicht zurueckgeschrieben.

## Code-Stelle (aktuelles `main`)

```python
if end_datetime:
    end_datetime.add(seconds=1)
```

## Problem

Bei nicht-mutierendem Datetime-Objekt (z. B. pendulum-typisches Verhalten) hat der Aufruf keinen Effekt.

## Erwartetes Verhalten

Zuweisung des Ergebnisses, z. B.:

```python
if end_datetime:
    end_datetime = end_datetime.add(seconds=1)
```

## Vermuteter Impact

Exklusives End-Intervall wird evtl. nicht korrekt erweitert (moegliche Trunkierung am Ende).

## Referenz

https://raw.githubusercontent.com/Akkudoktor-EOS/EOS/main/src/akkudoktoreos/core/dataabc.py

### EN - Title

`PredictionContainer.key_to_dataframe`: result of `end_datetime.add(seconds=1)` is not assigned

### EN - Body (Markdown)

## Summary

In `src/akkudoktoreos/core/dataabc.py`, `end_datetime.add(seconds=1)` is called but the result is not assigned.

## Code (current `main`)

```python
if end_datetime:
    end_datetime.add(seconds=1)
```

## Problem

If the datetime object is non-mutating (common pendulum-style behavior), this call has no effect.

## Expected behavior

Assign the returned value, e.g.:

```python
if end_datetime:
    end_datetime = end_datetime.add(seconds=1)
```

## Potential impact

Exclusive end-window handling may be wrong, causing end-of-range truncation.

## Reference

https://raw.githubusercontent.com/Akkudoktor-EOS/EOS/main/src/akkudoktoreos/core/dataabc.py

## EOS-UPSTREAM-004

### DE - Titel

`date_time` Prediction-Serie enthaelt nicht-uniformen Schritt (ein 105-Minuten-Sprung)

### DE - Body (Markdown)

## Zusammenfassung

Bei `/v1/prediction/list?key=date_time` wurde eine nicht-uniforme Zeitachse beobachtet:

- 94 Intervalle a 60 Minuten
- 1 Intervall a 105 Minuten
- Sprung: `2026-02-21T22:00:00Z -> 2026-02-21T23:45:00Z`

## Reproduktion

```bash
python3 - <<'PY'
import json,subprocess,datetime
vals=json.loads(subprocess.check_output(['curl','-sS','http://127.0.0.1:8503/v1/prediction/list?key=date_time']))
rows=[]
for v in vals:
    iv=int(v)
    sec=iv//1_000_000_000
    ns=iv%1_000_000_000
    rows.append(datetime.datetime.fromtimestamp(sec,datetime.timezone.utc)+datetime.timedelta(microseconds=round(ns/1000)))
rows.sort()
for i in range(1,len(rows)):
    d=(rows[i]-rows[i-1]).total_seconds()/60
    if d!=60:
        print(i-1, i, d, rows[i-1].isoformat(), rows[i].isoformat())
PY
```

## Frage an euch

Ist dieses Verhalten fuer den Provider-/Prediction-Pfad so beabsichtigt (Alignment/Interpolation), oder handelt es sich um eine Unstetigkeit im Zeitraster?

## Kontext

Beobachtet am 2026-02-21.

### EN - Title

`date_time` prediction series shows non-uniform step (single 105-minute jump)

### EN - Body (Markdown)

## Summary

Observed non-uniform timestamps in `/v1/prediction/list?key=date_time`:

- 94 intervals of 60 minutes
- 1 interval of 105 minutes
- transition: `2026-02-21T22:00:00Z -> 2026-02-21T23:45:00Z`

## Reproduction

```bash
python3 - <<'PY'
import json,subprocess,datetime
vals=json.loads(subprocess.check_output(['curl','-sS','http://127.0.0.1:8503/v1/prediction/list?key=date_time']))
rows=[]
for v in vals:
    iv=int(v)
    sec=iv//1_000_000_000
    ns=iv%1_000_000_000
    rows.append(datetime.datetime.fromtimestamp(sec,datetime.timezone.utc)+datetime.timedelta(microseconds=round(ns/1000)))
rows.sort()
for i in range(1,len(rows)):
    d=(rows[i]-rows[i-1]).total_seconds()/60
    if d!=60:
        print(i-1, i, d, rows[i-1].isoformat(), rows[i].isoformat())
PY
```

## Question

Is this expected provider/prediction alignment behavior, or an unintended irregularity in the time grid?

## Context

Observed on 2026-02-21.
