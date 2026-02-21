# EOS Upstream Findings (Internal)

Purpose: Internal tracking for possible bugs in Akkudoktor-EOS before opening upstream issues/PRs.

Issue draft templates (DE/EN) for all findings:

- `docs/eos-upstream-issue-drafts.md`

## Status Legend

- `candidate`: observed behavior, needs upstream confirmation.
- `likely_bug`: strong code-level evidence.
- `workaround_active`: EOS-Webapp already mitigates impact.

## EOS-UPSTREAM-001

- Title: `solution.prediction.data` horizon shorter than configured horizon / stored prediction series
- Status: `candidate`, `workaround_active`
- Observed at: 2026-02-21 (run `#177`)

### Observation

Configured run context showed 96h target horizon:

- `prediction.hours=96`
- `optimization.horizon_hours=96`

But solution payload (`/api/eos/runs/177/solution`) contained only 47 prediction points.

At the same time, stored prediction series for the same run had 96 points.

### Repro (local)

```bash
curl -s http://127.0.0.1:8080/api/eos/runs/177/context | jq '{prediction_hours:(.runtime_config_snapshot_json.prediction.hours), optimization_horizon_hours:(.runtime_config_snapshot_json.optimization.horizon_hours)}'
curl -s http://127.0.0.1:8080/api/eos/runs/177/solution | jq '.payload_json.prediction.data | length'
curl -s http://127.0.0.1:8080/api/eos/runs/177/prediction-series | jq '.points | length'
```

### Impact

- UI chart horizon could appear too short (`~47h`) despite configured `96h`.

### Webapp Workaround

- Use normalized run `prediction_series` artifact data for charts when available.

## EOS-UPSTREAM-002

- Title: `end_datetime` selection uses `provider.min_datetime` where `provider.max_datetime` is expected
- Status: `likely_bug`
- Source:
  - `src/akkudoktoreos/core/dataabc.py`
  - `PredictionContainer.key_to_dataframe(...)`

### Evidence

Current upstream code in EOS `main`:

```python
elif (
    provider.max_datetime
    and compare_datetimes(provider.max_datetime, end_datetime).gt
):
    end_datetime = provider.min_datetime
```

This assignment appears semantically wrong and likely should assign `provider.max_datetime`.

### Link

- https://raw.githubusercontent.com/Akkudoktor-EOS/EOS/main/src/akkudoktoreos/core/dataabc.py

## EOS-UPSTREAM-003

- Title: `end_datetime.add(seconds=1)` result is not assigned
- Status: `likely_bug`
- Source:
  - `src/akkudoktoreos/core/dataabc.py`
  - same method as EOS-UPSTREAM-002

### Evidence

Current upstream code:

```python
if end_datetime:
    end_datetime.add(seconds=1)
```

`DateTime.add(...)` is non-mutating in common datetime libs used by EOS (`pendulum`-style), so this likely has no effect.
Potentially causes exclusive-end truncation behavior.

### Link

- https://raw.githubusercontent.com/Akkudoktor-EOS/EOS/main/src/akkudoktoreos/core/dataabc.py

## EOS-UPSTREAM-004

- Title: `date_time` prediction key shows non-uniform 1h series (one 105min jump)
- Status: `candidate`
- Observed at: 2026-02-21 (`/v1/prediction/list?key=date_time`)

### Observation

- 94 intervals at 60 minutes.
- 1 interval at 105 minutes.
- Transition seen: `2026-02-21T22:00:00Z -> 2026-02-21T23:45:00Z`.

### Repro

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

### Notes

- Could be provider-side behavior (data source alignment) and not necessarily a core bug.
- Needs upstream clarification.

## Next Step Suggestion (Internal)

Before opening upstream issues, capture one fresh run with:

1. `prediction.hours=96`
2. raw `/v1/prediction/list?key=date_time`
3. `/v1/energy-management/optimization/solution` payload snapshot
4. relevant `prediction_series` artifacts

Then open 1-2 focused upstream issues (core code defect separately from data irregularity).
