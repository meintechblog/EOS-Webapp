from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.core.config import Settings
from app.services.eos_client import EosApiError, EosHealthSnapshot
from app.services.eos_orchestrator import (
    EosOrchestratorService,
    _extract_prediction_datetime_index,
    _extract_prediction_points,
    _extract_prediction_values,
    _is_legacy_no_solution_error,
    _merge_prediction_points,
    _is_retryable_eos_exception,
)


@contextmanager
def _unused_session_factory_context():
    yield object()


class _UnusedSessionFactory:
    def __call__(self):
        return _unused_session_factory_context()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _PriceBackfillClient:
    def __init__(
        self,
        *,
        before_history_hours: float,
        after_history_hours: float | None = None,
        recover_after_restart: bool = True,
    ) -> None:
        self.before_history_hours = before_history_hours
        self.after_history_hours = after_history_hours
        self.recover_after_restart = recover_after_restart
        self.restart_calls = 0
        self.refresh_provider_calls: list[str] = []
        self.feedin_refresh_calls = 0
        self._use_after_data = False

    def get_prediction_series(
        self,
        *,
        key: str,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
    ) -> dict:
        now = end_datetime or datetime.now(timezone.utc)
        history_hours = self.before_history_hours
        if self._use_after_data and self.after_history_hours is not None:
            history_hours = self.after_history_hours
        oldest = now - timedelta(hours=history_hours)
        return {
            "data": {
                _iso(oldest): 0.0001,
                _iso(now): 0.0002,
            }
        }

    def restart_server(self) -> dict:
        self.restart_calls += 1
        return {"message": "Restarting EOS"}

    def get_health(self) -> EosHealthSnapshot:
        if self.restart_calls > 0 and not self.recover_after_restart:
            raise RuntimeError("EOS unavailable")
        return EosHealthSnapshot(payload={"ok": True}, eos_last_run_datetime=None)

    def get_config(self) -> dict:
        return {
            "elecprice": {"provider": "ElecPriceEnergyCharts"},
            "feedintariff": {"provider": "FeedInTariffImport"},
        }

    def trigger_prediction_update_provider(
        self,
        *,
        provider_id: str,
        force_update: bool = False,
        force_enable: bool = False,
    ) -> None:
        if provider_id == "FeedInTariffImport":
            self.feedin_refresh_calls += 1
            return
        self.refresh_provider_calls.append(provider_id)
        self._use_after_data = True

    def put_config_path(self, path: str, value: object) -> object:
        return {"path": path, "saved": True}

    def save_config_file(self) -> dict:
        return {"saved": True}

    def get_prediction_list(
        self,
        *,
        key: str,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
        interval: str | None = None,
    ) -> list[float]:
        return [0.0001, 0.0002]


def _build_service(client: _PriceBackfillClient, **setting_overrides: object) -> EosOrchestratorService:
    settings = Settings(
        eos_price_backfill_enabled=True,
        eos_price_backfill_target_hours=672,
        eos_price_backfill_min_history_hours=648,
        eos_price_backfill_cooldown_seconds=86400,
        eos_price_backfill_restart_timeout_seconds=10,
        **setting_overrides,
    )
    return EosOrchestratorService(
        settings=settings,
        session_factory=_UnusedSessionFactory(),
        eos_client=client,  # type: ignore[arg-type]
    )


class PriceBackfillTests(TestCase):
    def test_merge_prediction_points_keeps_history_and_prefers_primary_on_conflict(self) -> None:
        jan_point = datetime(2026, 1, 24, 14, 15, tzinfo=timezone.utc)
        now_point = datetime(2026, 2, 21, 14, 0, tzinfo=timezone.utc)
        future_point = datetime(2026, 2, 21, 14, 15, tzinfo=timezone.utc)

        history = [
            (jan_point, 0.00012),
            (now_point, 0.00020),
        ]
        regular = [
            (now_point, 0.00022),
            (future_point, 0.00025),
        ]

        merged = _merge_prediction_points(regular, history)
        self.assertEqual([item[0] for item in merged], [jan_point, now_point, future_point])
        self.assertEqual(merged[1][1], 0.00022)

    def test_parser_supports_series_data_map(self) -> None:
        payload = {
            "data": {
                "2026-02-01T00:00:00Z": {"elecprice_marketprice_wh": 0.0001},
                "2026-02-01T01:00:00Z": {"value": 0.0002},
            }
        }
        points = _extract_prediction_points(payload)
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0][1], 0.0001)
        self.assertEqual(points[1][1], 0.0002)

        datetimes = _extract_prediction_datetime_index(payload)
        self.assertEqual(len(datetimes), 2)

        values = _extract_prediction_values(payload)
        self.assertEqual(values, [0.0001, 0.0002])

    def test_backfill_skips_when_history_is_sufficient(self) -> None:
        client = _PriceBackfillClient(before_history_hours=660.0)
        service = _build_service(client)

        result = service._maybe_backfill_price_history(scope="prices")  # type: ignore[attr-defined]
        self.assertIsNotNone(result)
        self.assertFalse(bool(result.get("applied")))
        self.assertTrue(bool(result.get("success")))
        self.assertEqual(result.get("status"), "sufficient_history")
        self.assertEqual(client.restart_calls, 0)
        self.assertEqual(client.refresh_provider_calls, [])

    def test_backfill_runs_when_history_too_short_and_no_cooldown(self) -> None:
        client = _PriceBackfillClient(before_history_hours=12.0, after_history_hours=670.0)
        service = _build_service(client)

        result = service._maybe_backfill_price_history(scope="prices")  # type: ignore[attr-defined]
        self.assertIsNotNone(result)
        self.assertTrue(bool(result.get("applied")))
        self.assertTrue(bool(result.get("success")))
        self.assertEqual(result.get("status"), "backfill_succeeded")
        self.assertEqual(client.restart_calls, 1)
        self.assertEqual(client.refresh_provider_calls, ["ElecPriceEnergyCharts"])

    def test_backfill_respects_cooldown(self) -> None:
        client = _PriceBackfillClient(before_history_hours=12.0)
        service = _build_service(client)
        with service._lock:  # type: ignore[attr-defined]
            service._price_backfill_cooldown_until_ts = datetime.now(timezone.utc) + timedelta(hours=1)  # type: ignore[attr-defined]

        result = service._maybe_backfill_price_history(scope="prices")  # type: ignore[attr-defined]
        self.assertIsNotNone(result)
        self.assertFalse(bool(result.get("applied")))
        self.assertEqual(result.get("status"), "cooldown_active")
        self.assertEqual(client.restart_calls, 0)

    def test_restart_timeout_is_reported_as_partial_reason(self) -> None:
        client = _PriceBackfillClient(before_history_hours=12.0)
        service = _build_service(client)

        captured: dict[str, str | None] = {}
        dummy_run = SimpleNamespace()
        refresh_payload = {
            "scope": "prices",
            "force_update": True,
            "force_enable": False,
            "providers": ["ElecPriceEnergyCharts"],
            "failed": [],
            "global_error": None,
            "price_history_backfill": {
                "applied": True,
                "success": False,
                "status": "error",
                "error": "EOS recovery timeout after restart (2s)",
            },
        }

        with patch.object(service, "_capture_run_input_snapshot", return_value=None), patch.object(
            service,
            "_capture_prediction_artifacts",
            return_value=None,
        ), patch.object(service, "_trigger_prediction_refresh", return_value=refresh_payload), patch(
            "app.services.eos_orchestrator.add_artifact",
            return_value=None,
        ), patch(
            "app.services.eos_orchestrator.get_run_by_id",
            return_value=dummy_run,
        ), patch(
            "app.services.eos_orchestrator.update_run_status",
        ) as update_run_status:
            service._prediction_refresh_worker(run_id=123, scope="prices")  # type: ignore[attr-defined]
            kwargs = update_run_status.call_args.kwargs
            captured["status"] = kwargs.get("status")
            captured["error_text"] = kwargs.get("error_text")

        self.assertEqual(captured["status"], "partial")
        self.assertIn("price history backfill", str(captured["error_text"]))
        self.assertIn("timeout", str(captured["error_text"]).lower())

    def test_runtime_snapshot_exposes_backfill_collector_fields(self) -> None:
        client = _PriceBackfillClient(before_history_hours=12.0)
        service = _build_service(client)
        now = datetime.now(timezone.utc)
        with service._lock:  # type: ignore[attr-defined]
            service._price_backfill_last_check_ts = now  # type: ignore[attr-defined]
            service._price_backfill_last_attempt_ts = now - timedelta(minutes=1)  # type: ignore[attr-defined]
            service._price_backfill_last_success_ts = now - timedelta(minutes=2)  # type: ignore[attr-defined]
            service._price_backfill_last_status = "backfill_succeeded"  # type: ignore[attr-defined]
            service._price_backfill_last_history_hours = 666.0  # type: ignore[attr-defined]
            service._price_backfill_cooldown_until_ts = now + timedelta(hours=1)  # type: ignore[attr-defined]

        runtime = service.get_runtime_snapshot()
        collector = runtime.get("collector", {})
        self.assertIn("price_backfill_last_check_ts", collector)
        self.assertIn("price_backfill_last_attempt_ts", collector)
        self.assertIn("price_backfill_last_success_ts", collector)
        self.assertIn("price_backfill_last_status", collector)
        self.assertIn("price_backfill_last_history_hours", collector)
        self.assertIn("price_backfill_cooldown_until_ts", collector)

    def test_call_eos_with_retry_retries_transient_errors(self) -> None:
        client = _PriceBackfillClient(before_history_hours=12.0)
        service = _build_service(client)
        attempts = {"count": 0}

        def flaky_call() -> dict[str, bool]:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise EosApiError(status_code=504, detail="timed out")
            return {"ok": True}

        result = service._call_eos_with_retry(  # type: ignore[attr-defined]
            action="test.retry",
            call=flaky_call,
            max_attempts=3,
            initial_delay_seconds=0.0,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(attempts["count"], 2)

    def test_retryable_classifier_distinguishes_transient_errors(self) -> None:
        transient = EosApiError(status_code=503, detail="connection refused")
        non_transient = EosApiError(status_code=400, detail="validation failed")

        self.assertTrue(_is_retryable_eos_exception(transient))
        self.assertFalse(_is_retryable_eos_exception(non_transient))

    def test_fetch_optional_run_artifact_waits_until_available(self) -> None:
        client = _PriceBackfillClient(before_history_hours=12.0)
        service = _build_service(
            client,
            eos_run_artifact_wait_seconds=3,
            eos_run_artifact_poll_seconds=1,
        )
        attempts = {"count": 0}

        def delayed_fetch() -> dict[str, str]:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise EosApiError(status_code=404, detail='{"detail":"not ready"}')
            return {"status": "ready"}

        with patch.object(service._stop_event, "wait", return_value=False):
            payload, missing_reason = service._fetch_optional_run_artifact_json(  # type: ignore[attr-defined]
                artifact_name="plan",
                fetcher=delayed_fetch,
            )

        self.assertEqual(payload, {"status": "ready"})
        self.assertIsNone(missing_reason)
        self.assertEqual(attempts["count"], 2)

    def test_fetch_optional_run_artifact_returns_missing_after_deadline(self) -> None:
        client = _PriceBackfillClient(before_history_hours=12.0)
        service = _build_service(
            client,
            eos_run_artifact_wait_seconds=0,
            eos_run_artifact_poll_seconds=1,
        )

        def always_missing() -> dict[str, str]:
            raise EosApiError(status_code=404, detail='{"detail":"not ready"}')

        payload, missing_reason = service._fetch_optional_run_artifact_json(  # type: ignore[attr-defined]
            artifact_name="solution",
            fetcher=always_missing,
        )

        self.assertIsNone(payload)
        self.assertIsNotNone(missing_reason)
        self.assertIn("not ready", str(missing_reason).lower())

    def test_legacy_no_solution_error_classifier(self) -> None:
        positive = EosApiError(
            status_code=400,
            detail='{"detail":"Optimize error: no solution stored by run."}',
        )
        negative = EosApiError(status_code=400, detail='{"detail":"other"}')

        self.assertTrue(_is_legacy_no_solution_error(positive))
        self.assertFalse(_is_legacy_no_solution_error(negative))
