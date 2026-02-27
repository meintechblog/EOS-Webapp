from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any
from unittest import TestCase

from app.core.config import Settings
from app.services.eos_client import EosHealthSnapshot
from app.services.eos_orchestrator import (
    EosOrchestratorService,
    _is_valid_pv_fallback_provider,
)


@contextmanager
def _unused_session_factory_context():
    yield object()


class _UnusedSessionFactory:
    def __call__(self):
        return _unused_session_factory_context()


def _build_pv_config(import_json: Any) -> dict[str, Any]:
    return {
        "pvforecast": {
            "provider": "PVForecastAkkudoktor",
            "providers": ["PVForecastAkkudoktor", "PVForecastImport"],
            "provider_settings": {
                "PVForecastImport": {
                    "import_json": import_json,
                }
            },
        }
    }


class _FallbackClient:
    def __init__(self, config_payload: dict[str, Any]) -> None:
        self.config_payload = copy.deepcopy(config_payload)
        self.put_calls: list[tuple[str, Any]] = []

    def get_health(self) -> EosHealthSnapshot:
        return EosHealthSnapshot(payload={"ok": True}, eos_last_run_datetime=None)

    def get_config(self) -> dict[str, Any]:
        return copy.deepcopy(self.config_payload)

    def put_config_path(self, path: str, value: Any) -> dict[str, Any]:
        self.put_calls.append((path, value))
        parts = [part for part in path.split("/") if part]
        current: Any = self.config_payload
        for part in parts[:-1]:
            current = current[part]
        current[parts[-1]] = value
        return {"path": path, "value": value}


def _build_service(client: _FallbackClient) -> EosOrchestratorService:
    settings = Settings(
        eos_prediction_pv_import_fallback_enabled=True,
        eos_prediction_pv_import_provider="PVForecastImport",
        eos_price_backfill_enabled=False,
        eos_feedin_spot_mirror_enabled=False,
    )
    return EosOrchestratorService(
        settings=settings,
        session_factory=_UnusedSessionFactory(),
        eos_client=client,  # type: ignore[arg-type]
    )


class PvFallbackValidationTests(TestCase):
    def test_helper_rejects_binary_import_profile(self) -> None:
        config_payload = _build_pv_config({"pvforecast_ac_power": [0.0] * 24 + [12000.0] * 24})

        valid, reason = _is_valid_pv_fallback_provider(config_payload, "PVForecastImport")

        self.assertFalse(valid)
        self.assertIn("too few unique values", reason or "")

    def test_attempt_fallback_requires_usable_import_profile(self) -> None:
        config_payload = _build_pv_config({"pvforecast_ac_power": [0.0] * 24 + [12000.0] * 24})
        client = _FallbackClient(config_payload)
        service = _build_service(client)

        result = service._attempt_pv_import_fallback(  # type: ignore[attr-defined]
            error_text="Provider PVForecastAkkudoktor fails on update",
            scope="all",
            failed_provider_id="PVForecastAkkudoktor",
        )

        self.assertIsNotNone(result)
        self.assertFalse(bool(result.get("applied")))
        self.assertIn("usable import data", str(result.get("note")))
        self.assertEqual(client.put_calls, [])
        self.assertEqual(client.config_payload["pvforecast"]["provider"], "PVForecastAkkudoktor")

    def test_fallback_provider_switch_is_restored_after_refresh(self) -> None:
        varied_profile = [float((index % 24) * 250) for index in range(48)]
        config_payload = _build_pv_config({"pvforecast_ac_power": varied_profile})
        client = _FallbackClient(config_payload)
        service = _build_service(client)

        fallback = service._attempt_pv_import_fallback(  # type: ignore[attr-defined]
            error_text="Provider PVForecastAkkudoktor fails on update",
            scope="all",
            failed_provider_id="PVForecastAkkudoktor",
        )

        self.assertIsNotNone(fallback)
        self.assertTrue(bool(fallback.get("applied")))
        self.assertEqual(client.config_payload["pvforecast"]["provider"], "PVForecastImport")

        restore_results = service._restore_pv_provider_after_fallback(  # type: ignore[attr-defined]
            fallback_applied=[fallback],
        )

        self.assertEqual(len(restore_results), 1)
        self.assertEqual(restore_results[0].get("status"), "restored")
        self.assertEqual(client.config_payload["pvforecast"]["provider"], "PVForecastAkkudoktor")
