# EOS-Webapp — Sprint 0/1 Plan

## Sprint 0 (Foundation)

1. Create private GitHub repository `EOS-Webapp`
2. Provision Proxmox VM (Linux)
3. Install Docker + Docker Compose
4. Bring up base stack
   - EOS service
   - Postgres
   - Backend scaffold
   - Frontend scaffold
5. Verify health endpoints and local network access

## Sprint 1 (First end-to-end value)

1. MQTT broker integration (`192.168.3.8`)
2. Input mapping model
   - User can map EOS input field -> MQTT topic
   - Optional payload path/transformation metadata
3. UI (left pane)
   - topic mapping editor
   - live value + last seen + status
4. Persist incoming MQTT telemetry in DB
5. Trigger EOS optimization run from UI
6. Display optimization result in right pane

## Non-goals (for now)

- Multi-user support
- Authentication/authorization
- Automated Influx backfill in app

## Public-ready constraints from day one

- `.env.example`
- clean secrets handling
- reproducible docker setup
- install docs + quickstart
- OSS-friendly structure
