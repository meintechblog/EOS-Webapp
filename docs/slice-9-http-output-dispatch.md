# Slice 9: Output-Signale (HTTP Pull, zentral)

Der frühere HTTP-Dispatch-Ansatz (Push/Webhook, Target-CRUD, Dispatch-Log) ist abgelöst.

Aktueller Stand:

- zentrale Pull-URL: `GET /eos/get/outputs`
- ein Request liefert alle aktuellen Soll-Leistungen je Resource
- Werte sind `kW` (signed)
- keine Dispatch-Worker, keine Output-Targets, kein Dispatch-Log

## Antwortformate

### 1) Loxone-Format (Default)

`GET /eos/get/outputs`

`text/plain`, je Signal eine Zeile:

```text
battery1_target_power_kw:0.0
shaby_target_power_kw:0.0
```

Für Loxone als Befehlskennung:

- `battery1_target_power_kw:\v`
- `shaby_target_power_kw:\v`

### 2) JSON-Debug

`GET /eos/get/outputs?format=json`

- enthält `central_http_path`, `run_id`, `fetched_at`
- enthält `signals`-Map inkl. Status, JSON-Pfad, Last-Fetch-Metadaten

## UI

`Output-Signale (HTTP Pull)` zeigt:

- zentrale Loxone-URL
- kompakten Abruf-Status zur URL (letzter Abruf, Quelle, Abrufe)
- je Signal Key, Wert, JSON-Pfad, Loxone-Befehlskennung

Refresh-Intervall im Web-UI: **5s**.

## Relevante Endpoints

- `GET /api/eos/output-signals` (UI-Source-of-Truth, JSON-Bundle, zählt Fetches nicht hoch)
- `GET /eos/get/outputs` (externes Pull, zählt Fetches hoch)
- `GET /api/eos/outputs/current`
- `GET /api/eos/outputs/timeline`
- `GET /api/eos/runs/{id}/plausibility`

## Entfernt (Legacy)

- `GET /api/eos/outputs/events`
- `POST /api/eos/outputs/dispatch/force`
- `GET/POST/PUT /api/eos/output-targets`

