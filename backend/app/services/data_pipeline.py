from __future__ import annotations

import logging
from datetime import datetime, timezone
from threading import Event, Lock, Thread

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.repositories.signal_backbone import (
    DataPipelineStatus,
    JobRunSnapshot,
    get_data_pipeline_status,
    run_retention_job,
    run_rollup_job,
)


class DataPipelineService:
    def __init__(self, *, settings: Settings, session_factory: sessionmaker):
        self._settings = settings
        self._session_factory = session_factory
        self._logger = logging.getLogger("app.data_pipeline")
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._running = False
        self._last_rollup_attempt_ts: datetime | None = None
        self._last_retention_attempt_ts: datetime | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._stop_event.clear()
        self._thread = Thread(target=self._loop, name="data-pipeline", daemon=True)
        self._thread.start()
        self._logger.info(
            "started data pipeline rollup_job_seconds=%s retention_job_seconds=%s",
            self._settings.data_rollup_job_seconds,
            self._settings.data_retention_job_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        with self._lock:
            self._running = False

    def run_rollup_once(self) -> None:
        with self._session_factory() as db:
            run_rollup_job(db)
        with self._lock:
            self._last_rollup_attempt_ts = datetime.now(timezone.utc)

    def run_retention_once(self) -> None:
        with self._session_factory() as db:
            run_retention_job(db, settings=self._settings)
        with self._lock:
            self._last_retention_attempt_ts = datetime.now(timezone.utc)

    def get_status_snapshot(self, db: Session) -> dict[str, object]:
        pipeline = get_data_pipeline_status(db)
        with self._lock:
            return {
                "running": self._running and not self._stop_event.is_set(),
                "last_rollup_attempt_ts": _to_iso(self._last_rollup_attempt_ts),
                "last_retention_attempt_ts": _to_iso(self._last_retention_attempt_ts),
                "last_error": self._last_error,
                "last_rollup_run": _job_snapshot_to_dict(pipeline.last_rollup_run),
                "last_retention_run": _job_snapshot_to_dict(pipeline.last_retention_run),
                "raw_rows_24h": pipeline.raw_rows_24h,
                "rollup_rows_24h": pipeline.rollup_rows_24h,
                "signal_catalog_count": pipeline.signal_catalog_count,
            }

    def _loop(self) -> None:
        next_rollup = datetime.now(timezone.utc)
        next_retention = datetime.now(timezone.utc)

        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)
            try:
                if now >= next_rollup:
                    self.run_rollup_once()
                    next_rollup = now + _seconds(self._settings.data_rollup_job_seconds)
                if now >= next_retention:
                    self.run_retention_once()
                    next_retention = now + _seconds(self._settings.data_retention_job_seconds)
                with self._lock:
                    self._last_error = None
            except Exception as exc:
                self._logger.exception("data pipeline loop iteration failed")
                with self._lock:
                    self._last_error = str(exc)

            self._stop_event.wait(1.0)


def _seconds(value: int):
    from datetime import timedelta

    return timedelta(seconds=value)


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _job_snapshot_to_dict(snapshot: JobRunSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "job_name": snapshot.job_name,
        "started_at": _to_iso(snapshot.started_at),
        "finished_at": _to_iso(snapshot.finished_at),
        "status": snapshot.status,
        "affected_rows": snapshot.affected_rows,
        "details_json": snapshot.details_json,
        "error_text": snapshot.error_text,
    }
