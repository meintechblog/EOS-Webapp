from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.eos_runtime import router as eos_runtime_router
from app.core.config import Settings
from app.dependencies import get_eos_orchestrator_service
from app.services.eos_client import EosApiError, EosHealthSnapshot
from app.services.eos_orchestrator import (
    EosOrchestratorService,
    _auto_run_preset_to_state,
    _default_auto_run_state_from_settings,
)


class _RuntimeClient:
    def get_health(self) -> EosHealthSnapshot:
        return EosHealthSnapshot(payload={"ok": True}, eos_last_run_datetime=None)

    def get_config(self) -> dict[str, Any]:
        return {
            "ems": {
                "mode": "OPTIMIZATION",
                "interval": 900,
            }
        }


@contextmanager
def _unused_session_factory_context():
    yield object()


class _UnusedSessionFactory:
    def __call__(self):
        return _unused_session_factory_context()


def _build_service(**setting_overrides: Any) -> EosOrchestratorService:
    settings = Settings(**setting_overrides)
    return EosOrchestratorService(
        settings=settings,
        session_factory=_UnusedSessionFactory(),
        eos_client=_RuntimeClient(),  # type: ignore[arg-type]
    )


class AutoRunPresetMappingTests(TestCase):
    def test_auto_run_preset_mapping(self) -> None:
        self.assertEqual(_auto_run_preset_to_state("off"), (False, []))
        self.assertEqual(_auto_run_preset_to_state("15m"), (True, [0, 15, 30, 45]))
        self.assertEqual(_auto_run_preset_to_state("30m"), (True, [0, 30]))
        self.assertEqual(_auto_run_preset_to_state("60m"), (True, [0]))

    def test_settings_fallback_uses_existing_alignment(self) -> None:
        preset, enabled, slots = _default_auto_run_state_from_settings(
            Settings(
                eos_aligned_scheduler_enabled=True,
                eos_aligned_scheduler_minutes="0,30",
            )
        )
        self.assertEqual(preset, "30m")
        self.assertTrue(enabled)
        self.assertEqual(slots, [0, 30])


class AutoRunPreferencePersistenceTests(TestCase):
    def test_load_runtime_preferences_applies_persisted_preset(self) -> None:
        service = _build_service()

        with patch(
            "app.services.eos_orchestrator.get_runtime_preference",
            return_value=SimpleNamespace(value_json={"preset": "60m"}),
        ):
            service._load_auto_run_state_from_preferences()  # type: ignore[attr-defined]

        status = service.get_collector_status()
        self.assertEqual(status.get("auto_run_preset"), "60m")
        self.assertEqual(status.get("aligned_scheduler_minutes"), "0")
        self.assertTrue(bool(status.get("auto_run_enabled")))

    def test_update_auto_run_preset_persists_and_updates_runtime_state(self) -> None:
        service = _build_service()
        captured: dict[str, Any] = {}

        def _fake_upsert(db: Any, *, key: str, value_json: Any) -> SimpleNamespace:
            captured["key"] = key
            captured["value_json"] = value_json
            return SimpleNamespace(key=key, value_json=value_json)

        with patch(
            "app.services.eos_orchestrator.upsert_runtime_preference",
            side_effect=_fake_upsert,
        ):
            response = service.update_auto_run_preset(preset="30m")

        self.assertEqual(captured.get("key"), "auto_run_preset")
        self.assertEqual(captured.get("value_json"), {"preset": "30m"})
        self.assertEqual(response.get("preset"), "30m")
        self.assertEqual(response.get("applied_slots"), [0, 30])

        status = service.get_collector_status()
        self.assertEqual(status.get("auto_run_preset"), "30m")
        self.assertEqual(status.get("aligned_scheduler_minutes"), "0,30")
        self.assertEqual(status.get("auto_run_interval_minutes"), 30)


class AutoRunApiTests(TestCase):
    def test_put_runtime_auto_run_updates_collector_and_persists_across_restart(self) -> None:
        preference_store: dict[str, Any] = {}

        def _fake_get(db: Any, *, key: str) -> SimpleNamespace | None:
            if key not in preference_store:
                return None
            return SimpleNamespace(key=key, value_json=preference_store[key])

        def _fake_upsert(db: Any, *, key: str, value_json: Any) -> SimpleNamespace:
            preference_store[key] = value_json
            return SimpleNamespace(key=key, value_json=value_json)

        with patch(
            "app.services.eos_orchestrator.get_runtime_preference",
            side_effect=_fake_get,
        ), patch(
            "app.services.eos_orchestrator.upsert_runtime_preference",
            side_effect=_fake_upsert,
        ):
            service = _build_service()
            service._load_auto_run_state_from_preferences()  # type: ignore[attr-defined]

            app = FastAPI()
            app.include_router(eos_runtime_router)
            app.dependency_overrides[get_eos_orchestrator_service] = lambda: service
            client = TestClient(app)

            response = client.put("/api/eos/runtime/auto-run", json={"preset": "60m"})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload.get("preset"), "60m")
            self.assertEqual(payload.get("applied_slots"), [0])

            collector = payload["runtime"]["collector"]
            self.assertEqual(collector.get("auto_run_preset"), "60m")
            self.assertTrue(bool(collector.get("auto_run_enabled")))
            self.assertEqual(collector.get("auto_run_interval_minutes"), 60)

            restarted = _build_service()
            restarted._load_auto_run_state_from_preferences()  # type: ignore[attr-defined]
            restarted_status = restarted.get_collector_status()
            self.assertEqual(restarted_status.get("auto_run_preset"), "60m")
            self.assertEqual(restarted_status.get("aligned_scheduler_minutes"), "0")


class RunConcurrencyGuardTests(TestCase):
    def test_request_force_run_rejects_when_running_run_exists(self) -> None:
        service = _build_service()

        with patch(
            "app.services.eos_orchestrator.list_running_runs",
            return_value=[SimpleNamespace(id=99)],
        ), patch(
            "app.services.eos_orchestrator.create_run",
        ) as create_run:
            with self.assertRaises(RuntimeError) as ctx:
                service.request_force_run()

        self.assertIn("already in progress", str(ctx.exception))
        self.assertFalse(create_run.called)

    def test_poll_once_skips_collection_when_any_run_is_running(self) -> None:
        service = _build_service()
        service._eos_client.get_health = lambda: EosHealthSnapshot(  # type: ignore[method-assign]
            payload={"ok": True},
            eos_last_run_datetime=datetime.now(timezone.utc),
        )

        with patch(
            "app.services.eos_orchestrator.list_running_runs",
            return_value=[SimpleNamespace(id=77)],
        ), patch.object(
            service,
            "_collect_run_for_last_datetime",
        ) as collect_run:
            service._poll_once()  # type: ignore[attr-defined]

        self.assertFalse(collect_run.called)

    def test_poll_once_skips_collection_when_auto_run_disabled(self) -> None:
        service = _build_service(eos_aligned_scheduler_enabled=False)
        service._eos_client.get_health = lambda: EosHealthSnapshot(  # type: ignore[method-assign]
            payload={"ok": True},
            eos_last_run_datetime=datetime.now(timezone.utc),
        )

        with patch.object(
            service,
            "_collect_run_for_last_datetime",
        ) as collect_run:
            service._poll_once()  # type: ignore[attr-defined]

        self.assertFalse(collect_run.called)

    def test_request_force_run_rejects_during_eos_warmup(self) -> None:
        service = _build_service(eos_artifact_warmup_grace_seconds=900)
        now = datetime.now(timezone.utc)
        warmup_health_payload = {
            "energy-management": {
                "start_datetime": now.isoformat(),
            }
        }
        warmup_exc = EosApiError(
            status_code=404,
            detail='{"detail":"Can not get the energy management plan.\\nDid you configure automatic optimization?"}',
        )

        service._eos_client.get_health = lambda: EosHealthSnapshot(  # type: ignore[method-assign]
            payload=warmup_health_payload,
            eos_last_run_datetime=now,
        )
        service._eos_client.get_plan = lambda: (_ for _ in ()).throw(warmup_exc)  # type: ignore[method-assign]
        service._eos_client.get_solution = lambda: (_ for _ in ()).throw(warmup_exc)  # type: ignore[method-assign]

        with patch(
            "app.services.eos_orchestrator.list_running_runs",
            return_value=[],
        ), patch(
            "app.services.eos_orchestrator.create_run",
        ) as create_run:
            with self.assertRaises(RuntimeError) as ctx:
                service.request_force_run()

        self.assertIn("warm-up active", str(ctx.exception))
        self.assertFalse(create_run.called)

    def test_poll_once_defers_collection_during_eos_warmup(self) -> None:
        service = _build_service(eos_artifact_warmup_grace_seconds=900)
        now = datetime.now(timezone.utc)
        warmup_health_payload = {
            "energy-management": {
                "start_datetime": now.isoformat(),
            }
        }
        warmup_exc = EosApiError(
            status_code=404,
            detail='{"detail":"Can not get the optimization solution.\\nDid you configure automatic optimization?"}',
        )

        service._eos_client.get_health = lambda: EosHealthSnapshot(  # type: ignore[method-assign]
            payload=warmup_health_payload,
            eos_last_run_datetime=now,
        )
        service._eos_client.get_plan = lambda: (_ for _ in ()).throw(warmup_exc)  # type: ignore[method-assign]
        service._eos_client.get_solution = lambda: (_ for _ in ()).throw(warmup_exc)  # type: ignore[method-assign]

        with patch(
            "app.services.eos_orchestrator.list_running_runs",
            return_value=[],
        ), patch(
            "app.services.eos_orchestrator.get_run_by_eos_last_run_datetime",
            return_value=None,
        ), patch.object(
            service,
            "_collect_run_for_last_datetime",
        ) as collect_run:
            service._poll_once()  # type: ignore[attr-defined]

        self.assertFalse(collect_run.called)
