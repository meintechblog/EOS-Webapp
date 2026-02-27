# Slice 6: Multi-Channel Inputs (MQTT + HTTP-In)

Dieser Slice erweitert den Input-Pfad auf kanalbasierte Ingest-Profile.

## Enthalten

- Input-Channel-Profile (`mqtt`, `http`) mit CRUD
- Mapping auf `channel_id + input_key` (Legacy `mqtt_topic` bleibt kompatibel)
- HTTP-Ingest Endpoints
- Gemeinsame Discovery über alle Channel
- Automap mit Channel-Kontext
- Legacy `GET /api/discovered-topics` bleibt verfügbar

## Migration

```bash
docker compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

Migration `20260220_0008_input_channels_http_ingest.py` erstellt/ändert:

- `input_channels`
- `input_observations`
- `input_mappings.channel_id`
- Mapping-Constraint/Unique auf `(channel_id, mqtt_topic)`
- `signal_measurements_raw.source_type` um `http_input`
- Backfill: `mqtt-default`, `http-default`

## API

### Input Channels

- `GET /api/input-channels`
- `POST /api/input-channels`
- `PUT /api/input-channels/{id}`
- `DELETE /api/input-channels/{id}`

Beispiel MQTT-Channel:

```bash
curl -s -X POST http://192.168.3.157:8080/api/input-channels \
  -H "Content-Type: application/json" \
  -d '{
    "code": "mqtt-garage",
    "name": "MQTT Garage",
    "channel_type": "mqtt",
    "enabled": true,
    "is_default": false,
    "config_json": {
      "host": "192.168.3.8",
      "port": 1883,
      "client_id": "eos-webapp-garage",
      "qos": 0,
      "discovery_topic": "eos/#"
    }
  }' | jq
```

### Mappings (additiv kompatibel)

Legacy funktioniert weiterhin:

```bash
curl -s -X POST http://192.168.3.157:8080/api/mappings \
  -H "Content-Type: application/json" \
  -d '{
    "eos_field":"pv_power_w",
    "mqtt_topic":"eos/input/pv_power_w",
    "enabled":true
  }' | jq
```

Neues Channel-Format:

```bash
curl -s -X POST http://192.168.3.157:8080/api/mappings \
  -H "Content-Type: application/json" \
  -d '{
    "eos_field":"pv_power_w",
    "channel_id":1,
    "input_key":"eos/input/pv_power_kw",
    "value_multiplier":1000,
    "enabled":true
  }' | jq
```

### Discovery + Automap

- `GET /api/discovered-inputs?channel_type=all&active_only=true`
- `GET /api/discovered-topics` (MQTT-Legacy)
- `POST /api/mappings/automap`

Beispiel:

```bash
curl -s "http://192.168.3.157:8080/api/discovered-inputs?channel_type=all&active_only=true" | jq
curl -s -X POST "http://192.168.3.157:8080/api/mappings/automap?channel_type=all" | jq
```

### HTTP-Ingest

GET-Form 1:

```bash
curl -s "http://192.168.3.157:8080/eos/input/pv_power_kw=2.0" | jq
```

GET-Form 2:

```bash
curl -s "http://192.168.3.157:8080/eos/input/pv_power_kw?value=2.0" | jq
```

Optional mit Channel-Code im Pfad:

```bash
curl -s "http://192.168.3.157:8080/eos/input/http-default/pv_power_kw=2.0" | jq
```

Kanonischer POST:

```bash
curl -s -X POST http://192.168.3.157:8080/api/input/http/push \
  -H "Content-Type: application/json" \
  -d '{
    "channel_code":"http-default",
    "input_key":"eos/input/pv_power_kw",
    "value":2.0,
    "timestamp":"2026-02-20T12:00:00Z"
  }' | jq
```

## Status

`GET /status` enthält additiv:

- `input_channels`
- `input_discovery`
- bestehende `mqtt`/`discovery`-Sektionen bleiben erhalten.
