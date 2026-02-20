# Slice 4: Parameters-Stammdaten, Profile, Import/Export

## Scope

Slice 4 erweitert den Middle-Pane um ein vollständiges Parameters-Management:

1. Core-Formulare für Anlage, Tarife, Forecast.
2. Advanced JSON für vollständige `SettingsEOS`-Abdeckung.
3. Profilverwaltung mit Revisionen (`draft`/`applied`) in Postgres.
4. Strict Validate/Apply Richtung EOS.
5. Import/Export mit Preview, Diff und strikter Fehlerbehandlung.

## Migration

Neue Tabellen:

- `parameter_profiles`
- `parameter_profile_revisions`

Migration anwenden:

```bash
docker-compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

## API overview

- `GET /api/parameters/catalog`
- `GET /api/parameters/profiles`
- `POST /api/parameters/profiles`
- `PUT /api/parameters/profiles/{id}`
- `GET /api/parameters/profiles/{id}`
- `PUT /api/parameters/profiles/{id}/draft`
- `POST /api/parameters/profiles/{id}/validate`
- `POST /api/parameters/profiles/{id}/apply`
- `GET /api/parameters/profiles/{id}/export`
- `POST /api/parameters/profiles/{id}/import/preview`
- `POST /api/parameters/profiles/{id}/import/apply`

`/status` enthält additiv `parameters` mit aktivem Profil und Apply-Metadaten.

## Quick verification

1. Katalog und Profile:

```bash
curl -s http://192.168.3.157:8080/api/parameters/catalog | jq
curl -s http://192.168.3.157:8080/api/parameters/profiles | jq
```

2. Profil erstellen:

```bash
curl -s -X POST http://192.168.3.157:8080/api/parameters/profiles \
  -H "Content-Type: application/json" \
  -d '{
    "name":"Anlage-Testprofil",
    "description":"Smoke test",
    "clone_from_profile_id":1
  }' | jq
```

3. Draft validieren und anwenden:

```bash
curl -s -X POST http://192.168.3.157:8080/api/parameters/profiles/2/validate | jq
curl -s -X POST http://192.168.3.157:8080/api/parameters/profiles/2/apply \
  -H "Content-Type: application/json" \
  -d '{"set_active_profile":true}' | jq
```

4. Export/Import strict:

```bash
curl -s "http://192.168.3.157:8080/api/parameters/profiles/2/export?revision=draft&include_secrets=false" | jq

curl -s -X POST http://192.168.3.157:8080/api/parameters/profiles/2/import/preview \
  -H "Content-Type: application/json" \
  -d '{"package_json":{"format":"eos-webapp.parameters.v1","payload":{"unknown_field":123}}}' | jq
```

Erwartung für das Beispiel oben: `valid=false`, Fehler wegen unbekanntem Feld.

## UI behavior

- `Parameters`-Tab enthält:
  - Profilauswahl/Erstellung/Aktivierung
  - Core-Bereiche (Standort, PV, Geräte, Tarife/Last)
  - Advanced JSON Editor
  - Actions: Draft speichern, validieren, auf EOS anwenden
  - Import/Export mit Preview + Diff
- Bei Legacy Fixed Mappings zeigt die UI Hinweis: Parameters sind führend für EOS Automatic.

## Security/Export policy

- Export default: maskierte Secrets (`include_secrets=false`)
- Voll-Export nur explizit (`include_secrets=true`)
- Import mit maskierten Platzhaltern bleibt blockiert, bis echte Werte gesetzt sind
