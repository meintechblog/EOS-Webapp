from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from app.core.config import Settings
from app.services.eos_client import EosApiError, EosHealthSnapshot
from app.services.eos_orchestrator import (
    EosOrchestratorService,
    _build_device_soc_measurement_rows,
    _extract_legacy_start_solution,
)


class _FakeEosClient:
    def __init__(self, *, prediction_lists: dict[str, list[Any]], config_payload: dict[str, Any]) -> None:
        self._prediction_lists = prediction_lists
        self._config_payload = config_payload
        self.put_calls: list[tuple[str, Any]] = []
        self.save_calls = 0

    def get_prediction_list(self, *, key: str) -> list[Any]:
        return list(self._prediction_lists.get(key, []))

    def get_config(self) -> dict[str, Any]:
        return dict(self._config_payload)

    def get_health(self) -> EosHealthSnapshot:
        return EosHealthSnapshot(payload={"ok": True}, eos_last_run_datetime=None)

    def put_config_path(self, path: str, value: Any) -> dict[str, Any]:
        self.put_calls.append((path, value))
        current: dict[str, Any] = self._config_payload
        parts = path.split("/")
        for part in parts[:-1]:
            nested = current.get(part)
            if not isinstance(nested, dict):
                nested = {}
                current[part] = nested
            current = nested
        current[parts[-1]] = value
        return {"path": path, "value": value}

    def save_config_file(self) -> dict[str, Any]:
        self.save_calls += 1
        return {"saved": True}


class _NoopDb:
    def add(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def commit(self) -> None:
        return None


@contextmanager
def _unused_session_factory_context():
    yield _NoopDb()


class _UnusedSessionFactory:
    def __call__(self):
        return _unused_session_factory_context()


def _build_service(
    *,
    warm_start_solution: list[float] | None,
    config_payload: dict[str, Any] | None = None,
    prediction_lists: dict[str, list[Any]] | None = None,
    setting_overrides: dict[str, Any] | None = None,
) -> EosOrchestratorService:
    if prediction_lists is None:
        prediction_lists = {
            "pvforecast_ac_power": [0.0, 100.0, 200.0],
            "elecprice_marketprice_wh": [0.0002, 0.00025, 0.0003],
            "loadforecast_power_w": [400.0, 450.0, 500.0],
            "feed_in_tariff_wh": [0.0001, 0.0001, 0.0001],
        }
    if config_payload is None:
        config_payload = {
            "devices": {
                "batteries": [
                    {
                        "levelized_cost_of_storage_kwh": 0.12,
                    }
                ]
            }
        }
    settings_kwargs: dict[str, Any] = dict(setting_overrides or {})
    service = EosOrchestratorService(
        settings=Settings(**settings_kwargs),
        session_factory=_UnusedSessionFactory(),
        eos_client=_FakeEosClient(
            prediction_lists=prediction_lists,
            config_payload=config_payload,
        ),
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

    def test_build_legacy_payload_caps_series_to_safe_horizon(self) -> None:
        long_series = [float(index) for index in range(60)]
        prediction_lists = {
            "pvforecast_ac_power": list(long_series),
            "elecprice_marketprice_wh": [0.0001 + (value / 1000000.0) for value in long_series],
            "loadforecast_power_w": [500.0 + value for value in long_series],
            "feed_in_tariff_wh": [0.00005 + (value / 1000000.0) for value in long_series],
        }
        service = _build_service(
            warm_start_solution=[1.0 for _ in range(60)],
            prediction_lists=prediction_lists,
            setting_overrides={"eos_visualize_safe_horizon_hours": 48},
        )

        payload = service._build_legacy_optimize_payload()
        ems = payload["ems"]
        self.assertEqual(len(ems["pv_prognose_wh"]), 48)
        self.assertEqual(len(ems["strompreis_euro_pro_wh"]), 48)
        self.assertEqual(len(ems["gesamtlast"]), 48)
        self.assertEqual(len(ems["einspeiseverguetung_euro_pro_wh"]), 48)
        self.assertEqual(len(payload["start_solution"]), 48)

    def test_apply_safe_horizon_cap_updates_prediction_and_optimization(self) -> None:
        config_payload = {
            "prediction": {"hours": 96},
            "optimization": {"horizon_hours": 96},
            "devices": {"batteries": [{"levelized_cost_of_storage_kwh": 0.12}]},
        }
        service = _build_service(
            warm_start_solution=None,
            config_payload=config_payload,
            setting_overrides={"eos_visualize_safe_horizon_hours": 48},
        )

        changed = service._apply_safe_horizon_cap()  # type: ignore[attr-defined]

        self.assertTrue(changed)
        client = service._eos_client  # type: ignore[attr-defined]
        self.assertEqual(client._config_payload["prediction"]["hours"], 48)
        self.assertEqual(client._config_payload["optimization"]["horizon_hours"], 48)
        self.assertEqual(client.save_calls, 1)

    def test_apply_safe_horizon_cap_syncs_up_to_profile_target_when_cap_allows(self) -> None:
        config_payload = {
            "prediction": {"hours": 48},
            "optimization": {"horizon_hours": 48},
            "devices": {"batteries": [{"levelized_cost_of_storage_kwh": 0.12}]},
        }
        service = _build_service(
            warm_start_solution=None,
            config_payload=config_payload,
            setting_overrides={"eos_visualize_safe_horizon_hours": 96},
        )
        service._read_active_profile_horizon_targets = lambda: (96, 96)  # type: ignore[method-assign]

        changed = service._apply_safe_horizon_cap()  # type: ignore[attr-defined]

        self.assertTrue(changed)
        client = service._eos_client  # type: ignore[attr-defined]
        self.assertEqual(client._config_payload["prediction"]["hours"], 96)
        self.assertEqual(client._config_payload["optimization"]["horizon_hours"], 96)
        self.assertEqual(client.save_calls, 1)

    def test_apply_safe_horizon_cap_respects_cap_when_profile_target_is_higher(self) -> None:
        config_payload = {
            "prediction": {"hours": 48},
            "optimization": {"horizon_hours": 48},
            "devices": {"batteries": [{"levelized_cost_of_storage_kwh": 0.12}]},
        }
        service = _build_service(
            warm_start_solution=None,
            config_payload=config_payload,
            setting_overrides={"eos_visualize_safe_horizon_hours": 48},
        )
        service._read_active_profile_horizon_targets = lambda: (96, 96)  # type: ignore[method-assign]

        changed = service._apply_safe_horizon_cap()  # type: ignore[attr-defined]

        self.assertFalse(changed)
        client = service._eos_client  # type: ignore[attr-defined]
        self.assertEqual(client._config_payload["prediction"]["hours"], 48)
        self.assertEqual(client._config_payload["optimization"]["horizon_hours"], 48)
        self.assertEqual(client.save_calls, 0)

    def test_build_legacy_payload_includes_eauto_from_config(self) -> None:
        service = _build_service(
            warm_start_solution=None,
            config_payload={
                "devices": {
                    "batteries": [
                        {
                            "levelized_cost_of_storage_kwh": 0.12,
                        }
                    ],
                    "max_electric_vehicles": 1,
                    "electric_vehicles": [
                        {
                            "device_id": "shaby",
                            "capacity_wh": 70000,
                            "max_charge_power_w": 11000,
                            "min_soc_percentage": 0,
                            "max_soc_percentage": 80,
                            "charging_efficiency": 0.9,
                            "discharging_efficiency": 1.0,
                            "charge_rates": [0.0, 0.5, 1.0],
                        }
                    ],
                }
            },
        )
        payload = service._build_legacy_optimize_payload()
        self.assertIsInstance(payload["eauto"], dict)
        self.assertEqual(payload["eauto"]["device_id"], "shaby")
        self.assertEqual(payload["eauto"]["capacity_wh"], 70000)
        self.assertEqual(payload["eauto"]["max_charge_power_w"], 11000.0)
        self.assertEqual(payload["eauto"]["max_soc_percentage"], 80)

    def test_build_legacy_payload_uses_null_eauto_when_ev_disabled(self) -> None:
        service = _build_service(
            warm_start_solution=None,
            config_payload={
                "devices": {
                    "batteries": [
                        {
                            "levelized_cost_of_storage_kwh": 0.12,
                        }
                    ],
                    "max_electric_vehicles": 0,
                    "electric_vehicles": [
                        {
                            "device_id": "shaby",
                            "capacity_wh": 70000,
                        }
                    ],
                }
            },
        )
        payload = service._build_legacy_optimize_payload()
        self.assertIsNone(payload["eauto"])

    def test_force_run_worker_marks_partial_on_legacy_no_solution(self) -> None:
        service = _build_service(warm_start_solution=None)
        no_solution_error = EosApiError(
            status_code=400,
            detail='{"detail":"Optimize error: no solution stored by run."}',
        )
        captured: dict[str, Any] = {}
        fake_run = SimpleNamespace(status="running", error_text=None)

        service._trigger_prediction_refresh = lambda scope: {"global_error": None, "failed": []}  # type: ignore[method-assign]
        service._push_latest_measurements_to_eos = lambda: {"failed_count": 0}  # type: ignore[method-assign]
        service._set_runtime_mode_and_interval = lambda mode, interval_seconds: {"mode_path": "x", "interval_path": "y"}  # type: ignore[method-assign]
        service._wait_for_next_last_run_datetime = lambda previous, timeout_seconds: None  # type: ignore[method-assign]
        service._run_legacy_optimize = lambda run_id: (_ for _ in ()).throw(no_solution_error)  # type: ignore[method-assign]
        service._capture_run_input_snapshot = lambda run_id: None  # type: ignore[method-assign]

        with patch(
            "app.services.eos_orchestrator.get_run_by_id",
            return_value=fake_run,
        ), patch(
            "app.services.eos_orchestrator.update_run_status",
            side_effect=lambda db, run, status, error_text, finished_at: captured.update(
                {"status": status, "error_text": error_text}
            ),
        ):
            service._force_run_worker(
                run_id=123,
                trigger_source="automatic",
                run_mode="aligned_schedule",
            )

        self.assertEqual(captured.get("status"), "partial")
        self.assertIn("legacy optimize returned no solution", str(captured.get("error_text")))

    def test_force_run_worker_keeps_success_for_soft_pre_force_notes(self) -> None:
        service = _build_service(warm_start_solution=None)
        fake_run = SimpleNamespace(status="success", error_text=None)
        note_artifacts: list[dict[str, Any]] = []

        service._trigger_prediction_refresh = lambda scope: {  # type: ignore[method-assign]
            "global_error": "temporary provider DNS failure",
            "failed": [],
        }
        service._push_latest_measurements_to_eos = lambda: {"failed_count": 0}  # type: ignore[method-assign]
        service._set_runtime_mode_and_interval = lambda mode, interval_seconds: {"mode_path": "x", "interval_path": "y"}  # type: ignore[method-assign]
        service._wait_for_next_last_run_datetime = lambda previous, timeout_seconds: datetime.now(timezone.utc)  # type: ignore[method-assign]
        service._collect_run_for_last_datetime = lambda *args, **kwargs: 123  # type: ignore[method-assign]
        service._capture_run_input_snapshot = lambda run_id: None  # type: ignore[method-assign]

        def _capture_artifact(
            db: Any,
            *,
            run_id: int,
            artifact_type: str,
            artifact_key: str,
            payload_json: dict[str, Any] | list[Any],
            valid_from: datetime | None = None,
            valid_until: datetime | None = None,
        ) -> None:
            if artifact_type == "run_note":
                note_artifacts.append(
                    {
                        "run_id": run_id,
                        "artifact_key": artifact_key,
                        "payload_json": payload_json,
                    }
                )

        with patch(
            "app.services.eos_orchestrator.add_artifact",
            side_effect=_capture_artifact,
        ), patch(
            "app.services.eos_orchestrator.get_run_by_id",
            return_value=fake_run,
        ), patch(
            "app.services.eos_orchestrator.update_run_status",
        ) as update_run_status:
            service._force_run_worker(
                run_id=321,
                trigger_source="automatic",
                run_mode="aligned_schedule",
            )

        self.assertFalse(update_run_status.called)
        self.assertEqual(fake_run.status, "success")
        self.assertIsNone(fake_run.error_text)
        self.assertTrue(note_artifacts)
        warning_payload = note_artifacts[0]["payload_json"]
        self.assertEqual(warning_payload.get("severity"), "warning")
        self.assertIn("pre-force prediction refresh global error", " | ".join(warning_payload.get("notes", [])))

    def test_force_run_worker_keeps_partial_and_appends_soft_context(self) -> None:
        service = _build_service(warm_start_solution=None)
        fake_run = SimpleNamespace(status="partial", error_text="plan unavailable")

        service._trigger_prediction_refresh = lambda scope: {  # type: ignore[method-assign]
            "global_error": "temporary provider DNS failure",
            "failed": [],
        }
        service._push_latest_measurements_to_eos = lambda: {"failed_count": 0}  # type: ignore[method-assign]
        service._set_runtime_mode_and_interval = lambda mode, interval_seconds: {"mode_path": "x", "interval_path": "y"}  # type: ignore[method-assign]
        service._wait_for_next_last_run_datetime = lambda previous, timeout_seconds: datetime.now(timezone.utc)  # type: ignore[method-assign]
        service._collect_run_for_last_datetime = lambda *args, **kwargs: 123  # type: ignore[method-assign]
        service._capture_run_input_snapshot = lambda run_id: None  # type: ignore[method-assign]

        with patch(
            "app.services.eos_orchestrator.add_artifact",
            return_value=None,
        ), patch(
            "app.services.eos_orchestrator.get_run_by_id",
            return_value=fake_run,
        ), patch(
            "app.services.eos_orchestrator.update_run_status",
        ) as update_run_status:
            service._force_run_worker(
                run_id=322,
                trigger_source="automatic",
                run_mode="aligned_schedule",
            )

        self.assertFalse(update_run_status.called)
        self.assertEqual(fake_run.status, "partial")
        self.assertIn("plan unavailable", str(fake_run.error_text))
        self.assertIn("pre-force prediction refresh global error", str(fake_run.error_text))

    def test_collect_run_automatic_keeps_success_for_not_configured_plan_solution(self) -> None:
        service = _build_service(warm_start_solution=None)
        run_dt = datetime.now(timezone.utc)
        fake_run = SimpleNamespace(id=999, status="running", error_text=None)
        captured: dict[str, Any] = {}

        service._capture_run_input_snapshot = lambda run_id: None  # type: ignore[method-assign]
        service._capture_prediction_artifacts = lambda **kwargs: None  # type: ignore[method-assign]
        service._eos_client.get_health = lambda: EosHealthSnapshot(  # type: ignore[method-assign]
            payload={"status": "alive"},
            eos_last_run_datetime=run_dt,
        )
        service._eos_client.get_plan = lambda: {}  # type: ignore[attr-defined]
        service._eos_client.get_solution = lambda: {}  # type: ignore[attr-defined]

        def _fake_fetch_optional(
            *,
            artifact_name: str,
            fetcher: Any,
            wait_seconds_override: int | None = None,
        ) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
            del fetcher, wait_seconds_override
            if artifact_name == "plan":
                return None, "Can not get the energy management plan."
            if artifact_name == "solution":
                return None, "Can not get the optimization solution."
            return None, "not available"

        service._fetch_optional_run_artifact_json = _fake_fetch_optional  # type: ignore[method-assign]

        with patch(
            "app.services.eos_orchestrator.get_run_by_eos_last_run_datetime",
            return_value=None,
        ), patch(
            "app.services.eos_orchestrator.create_run",
            return_value=fake_run,
        ), patch(
            "app.services.eos_orchestrator.add_artifact",
            return_value=None,
        ), patch(
            "app.services.eos_orchestrator.get_run_by_id",
            return_value=fake_run,
        ), patch(
            "app.services.eos_orchestrator.update_run_status",
            side_effect=lambda db, run, status, error_text, finished_at: captured.update(
                {"status": status, "error_text": error_text}
            ),
        ), patch.object(
            service,
            "_persist_run_notes",
        ) as persist_notes:
            service._collect_run_for_last_datetime(
                run_dt,
                trigger_source="automatic",
                run_mode="aligned_schedule",
                existing_run_id=None,
            )

        self.assertEqual(captured.get("status"), "success")
        self.assertIsNone(captured.get("error_text"))
        self.assertTrue(persist_notes.called)

    def test_build_device_soc_measurement_rows_maps_generic_soc_pct_to_device_keys(self) -> None:
        now = datetime.now(timezone.utc)
        rows = _build_device_soc_measurement_rows(
            latest_soc=[
                {
                    "signal_key": "battery_soc_pct",
                    "last_ts": now,
                    "last_value_num": 74.0,
                }
            ],
            config_payload={
                "devices": {
                    "batteries": [
                        {
                            "device_id": "hausspeicher",
                            "measurement_key_soc_factor": "hausspeicher-soc-factor",
                        }
                    ],
                    "electric_vehicles": [
                        {
                            "device_id": "shaby",
                            "measurement_key_soc_factor": "shaby-soc-factor",
                        }
                    ],
                }
            },
            available_keys={"hausspeicher-soc-factor", "shaby-soc-factor"},
        )

        by_key = {str(item["key"]): float(item["value"]) for item in rows}
        self.assertAlmostEqual(by_key["hausspeicher-soc-factor"], 0.74, places=6)
        self.assertAlmostEqual(by_key["shaby-soc-factor"], 0.74, places=6)

    def test_build_device_soc_measurement_rows_prefers_exact_device_signal(self) -> None:
        now = datetime.now(timezone.utc)
        rows = _build_device_soc_measurement_rows(
            latest_soc=[
                {
                    "signal_key": "hausspeicher-soc-factor",
                    "last_ts": now,
                    "last_value_num": 0.61,
                },
                {
                    "signal_key": "battery_soc_pct",
                    "last_ts": now,
                    "last_value_num": 80.0,
                },
            ],
            config_payload={
                "devices": {
                    "batteries": [
                        {
                            "device_id": "hausspeicher",
                            "measurement_key_soc_factor": "hausspeicher-soc-factor",
                        }
                    ]
                }
            },
            available_keys={"hausspeicher-soc-factor"},
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "hausspeicher-soc-factor")
        self.assertAlmostEqual(float(rows[0]["value"]), 0.61, places=6)
