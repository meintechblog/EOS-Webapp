from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest import TestCase

from app.core.config import Settings
from app.services.eos_orchestrator import EosOrchestratorService, _extract_legacy_start_solution


class _FakeEosClient:
    def __init__(self, *, prediction_lists: dict[str, list[Any]], config_payload: dict[str, Any]) -> None:
        self._prediction_lists = prediction_lists
        self._config_payload = config_payload

    def get_prediction_list(self, *, key: str) -> list[Any]:
        return list(self._prediction_lists.get(key, []))

    def get_config(self) -> dict[str, Any]:
        return dict(self._config_payload)


@contextmanager
def _unused_session_factory_context():
    yield None


class _UnusedSessionFactory:
    def __call__(self):
        return _unused_session_factory_context()


def _build_service(*, warm_start_solution: list[float] | None) -> EosOrchestratorService:
    prediction_lists = {
        "pvforecast_ac_power": [0.0, 100.0, 200.0],
        "elecprice_marketprice_wh": [0.0002, 0.00025, 0.0003],
        "loadforecast_power_w": [400.0, 450.0, 500.0],
        "feed_in_tariff_wh": [0.0001, 0.0001, 0.0001],
    }
    config_payload = {
        "devices": {
            "batteries": [
                {
                    "levelized_cost_of_storage_kwh": 0.12,
                }
            ]
        }
    }
    service = EosOrchestratorService(
        settings=Settings(),
        session_factory=_UnusedSessionFactory(),
        eos_client=_FakeEosClient(
            prediction_lists=prediction_lists,
            config_payload=config_payload,
        ),
        mqtt_service=None,
    )
    service._load_latest_legacy_start_solution = lambda: warm_start_solution  # type: ignore[method-assign]
    return service


class LegacyWarmStartTests(TestCase):
    def test_extract_legacy_start_solution_accepts_numeric_values(self) -> None:
        payload = {"start_solution": [1, 0, "0.5"]}
        extracted = _extract_legacy_start_solution(payload)
        self.assertEqual(extracted, [1.0, 0.0, 0.5])

    def test_extract_legacy_start_solution_rejects_invalid_payload(self) -> None:
        self.assertIsNone(_extract_legacy_start_solution({"start_solution": [1]}))
        self.assertIsNone(_extract_legacy_start_solution({"start_solution": [1, "x"]}))
        self.assertIsNone(_extract_legacy_start_solution({"start_solution": None}))

    def test_build_legacy_payload_includes_warm_start_solution(self) -> None:
        service = _build_service(warm_start_solution=[1.0, 0.0, 1.0])
        payload = service._build_legacy_optimize_payload()
        self.assertEqual(payload["start_solution"], [1.0, 0.0, 1.0])

    def test_build_legacy_payload_uses_null_without_warm_start(self) -> None:
        service = _build_service(warm_start_solution=None)
        payload = service._build_legacy_optimize_payload()
        self.assertIsNone(payload["start_solution"])

