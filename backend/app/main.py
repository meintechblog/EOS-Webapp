from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="EOS-Webapp Backend")

PROGRESS_FILE = Path("/data/progress.log")
WORKLOG_FILE = Path("/data/worklog.md")


def _tail(path: Path, lines: int = 30) -> list[str]:
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return content[-lines:]


@app.get("/health")
def health():
    return {"status": "ok", "service": "backend"}


@app.get("/status")
def status():
    return {
        "status": "working",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "progress_tail": _tail(PROGRESS_FILE, 40),
        "worklog_tail": _tail(WORKLOG_FILE, 40),
    }


@app.get("/status/live", response_class=HTMLResponse)
def status_live():
    return """
<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <title>EOS-Webapp Live Status</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 20px; background: #111; color: #eee; }
    h1 { margin-bottom: 8px; }
    .muted { color: #aaa; }
    .card { border: 1px solid #333; border-radius: 8px; padding: 12px; margin-top: 12px; background: #181818; }
    pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.4; }
  </style>
</head>
<body>
  <h1>EOS-Webapp / Live Status</h1>
  <div class='muted'>Auto-refresh every 5s</div>
  <div class='card'><b>Timestamp:</b> <span id='ts'>...</span></div>
  <div class='card'>
    <h3>Progress Log (live)</h3>
    <pre id='progress'></pre>
  </div>
  <div class='card'>
    <h3>Worklog</h3>
    <pre id='worklog'></pre>
  </div>
  <script>
    async function load() {
      const res = await fetch('/status');
      const data = await res.json();
      document.getElementById('ts').textContent = data.timestamp;
      document.getElementById('progress').textContent = (data.progress_tail || []).join('\n') || 'No progress entries yet';
      document.getElementById('worklog').textContent = (data.worklog_tail || []).join('\n') || 'No worklog entries yet';
    }
    load();
    setInterval(load, 5000);
  </script>
</body>
</html>
    """
