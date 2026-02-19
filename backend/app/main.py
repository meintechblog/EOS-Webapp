from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.eos_fields import router as eos_fields_router
from app.api.live_values import router as live_values_router
from app.api.mappings import router as mappings_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal, check_db_connection, get_db
from app.services.eos_catalog import EosFieldCatalogService
from app.services.mqtt_ingest import MqttIngestService

PROGRESS_FILE = Path("/data/progress.log")
WORKLOG_FILE = Path("/data/worklog.md")


def _tail(path: Path, lines: int = 30) -> list[str]:
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return content[-lines:]


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    mqtt_service = MqttIngestService(settings=settings, session_factory=SessionLocal)
    eos_catalog_service = EosFieldCatalogService(settings=settings)
    app.state.settings = settings
    app.state.mqtt_service = mqtt_service
    app.state.eos_catalog_service = eos_catalog_service
    mqtt_service.start()
    mqtt_service.sync_subscriptions_from_db()
    try:
        yield
    finally:
        mqtt_service.stop()


app = FastAPI(title="EOS-Webapp Backend", lifespan=lifespan)
app.include_router(eos_fields_router)
app.include_router(mappings_router)
app.include_router(live_values_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "backend"}


@app.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    db_ok, db_error = check_db_connection(db)
    mqtt_service: MqttIngestService | None = getattr(request.app.state, "mqtt_service", None)
    settings: Settings | None = getattr(request.app.state, "settings", None)

    db_status: dict[str, object] = {"ok": db_ok}
    if db_error:
        db_status["error"] = db_error

    mqtt_status: dict[str, object]
    telemetry_status: dict[str, object]
    if mqtt_service is None:
        mqtt_status = {"connected": False, "error": "MQTT service not initialized"}
        telemetry_status = {"messages_received": 0, "last_message_ts": None}
    else:
        mqtt_status = mqtt_service.get_connection_status()
        telemetry_status = mqtt_service.get_telemetry_status()

    return {
        "status": "working",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "progress_tail": _tail(PROGRESS_FILE, 40),
        "worklog_tail": _tail(WORKLOG_FILE, 40),
        "db": db_status,
        "mqtt": mqtt_status,
        "telemetry": telemetry_status,
        "config": {
            "live_stale_seconds": settings.live_stale_seconds if settings else None,
        },
    }


def _live_html() -> str:
    progress = "\n".join(_tail(PROGRESS_FILE, 40)) or "No progress entries yet"
    worklog = "\n".join(_tail(WORKLOG_FILE, 40)) or "No worklog entries yet"
    ts = datetime.now(timezone.utc).isoformat()
    return f"""
<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <title>EOS-Webapp Live Status</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 20px; background: #111; color: #eee; }}
    h1 {{ margin-bottom: 8px; }}
    .muted {{ color: #aaa; }}
    .card {{ border: 1px solid #333; border-radius: 8px; padding: 12px; margin-top: 12px; background: #181818; }}
    pre {{ white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.4; }}
  </style>
</head>
<body>
  <h1>EOS-Webapp / Live Status</h1>
  <div class='muted'>Auto-refresh every 5s</div>
  <div class='card'><b>Timestamp:</b> <span id='ts'>{ts}</span></div>
  <div class='card'>
    <h3>DB / MQTT / Telemetry</h3>
    <pre id='runtime'>Loading runtime status...</pre>
  </div>
  <div class='card'>
    <h3>Progress Log (live)</h3>
    <pre id='progress'>{progress}</pre>
  </div>
  <div class='card'>
    <h3>Worklog</h3>
    <pre id='worklog'>{worklog}</pre>
  </div>
  <script>
    async function load() {{
      const res = await fetch('/status');
      const data = await res.json();
      document.getElementById('ts').textContent = data.timestamp;
      document.getElementById('progress').textContent = (data.progress_tail || []).join('\\n') || 'No progress entries yet';
      document.getElementById('worklog').textContent = (data.worklog_tail || []).join('\\n') || 'No worklog entries yet';
      document.getElementById('runtime').textContent = JSON.stringify({{
        db: data.db || null,
        mqtt: data.mqtt || null,
        telemetry: data.telemetry || null
      }}, null, 2);
    }}
    load();
    setInterval(load, 5000);
  </script>
</body>
</html>
    """


@app.get("/status/live", response_class=HTMLResponse)
def status_live():
    return _live_html()


@app.get("/stats/live", response_class=HTMLResponse)
def stats_live_alias():
    return _live_html()
