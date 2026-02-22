from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.eos_output_signals import router as eos_output_signals_router
from app.api.eos_runtime import router as eos_runtime_router
from app.core.config import Settings
from app.db.session import get_db
from app.dependencies import get_output_projection_service
from app.services.output_projection import (
    OutputProjectionService,
    _mode_direction,
)


def _instruction(
    *,
    instruction_id: int,
    run_id: int,
    resource_id: str,
    mode: str,
    factor: float,
) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=instruction_id,
        run_id=run_id,
        instruction_index=instruction_id,
        instruction_type="operation_mode",
        resource_id=resource_id,
        actuator_id=None,
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(minutes=20),
        execution_time=now - timedelta(minutes=1),
        operation_mode_id=mode,
        operation_mode_factor=factor,
        payload_json={"resource_id": resource_id, "operation_mode_id": mode, "operation_mode_factor": factor},
    )


class _AccessStateStore:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def list(self, _db: Any, *, signal_keys: list[str]) -> list[SimpleNamespace]:
        rows: list[SimpleNamespace] = []
        for key in signal_keys:
            row = self._rows.get(key)
            if row is None:
                continue
            rows.append(SimpleNamespace(**row))
        return rows

    def upsert(
        self,
        _db: Any,
        *,
        signal_key: str,
        resource_id: str | None,
        last_fetch_ts: datetime,
        last_fetch_client: str | None,
    ) -> SimpleNamespace:
        existing = self._rows.get(signal_key)
        fetch_count = int(existing["fetch_count"]) + 1 if existing is not None else 1
        row = {
            "signal_key": signal_key,
            "resource_id": resource_id,
            "last_fetch_ts": last_fetch_ts,
            "last_fetch_client": last_fetch_client,
            "fetch_count": fetch_count,
        }
        self._rows[signal_key] = row
        return SimpleNamespace(**row)


class OutputSignalDirectionTests(TestCase):
    def test_mode_direction_mapping_includes_ambiguous_modes(self) -> None:
        self.assertEqual(_mode_direction("FORCED_CHARGE"), 1)
        self.assertEqual(_mode_direction("GRID_SUPPORT_IMPORT"), 1)
        self.assertEqual(_mode_direction("FORCED_DISCHARGE"), -1)
        self.assertEqual(_mode_direction("GRID_SUPPORT_EXPORT"), -1)
        self.assertEqual(_mode_direction("SELF_CONSUMPTION"), 0)
        self.assertEqual(_mode_direction("NON_EXPORT"), 0)


class OutputSignalPowerMappingTests(TestCase):
    def test_extract_resource_max_power_map_from_runtime_snapshot(self) -> None:
        service = OutputProjectionService(settings=Settings())
        runtime_snapshot = {
            "devices": {
                "batteries": [
                    {
                        "max_charge_power_w": 5200,
                        "device_id": "shaby",
                    }
                ],
                "electric_vehicles": [
                    {
                        "max_charge_power_w": 11000,
                        "device_id": "ev_wallbox_1",
                    }
                ],
            }
        }

        with patch(
            "app.services.output_projection.get_run_input_snapshot",
            return_value=SimpleNamespace(runtime_config_snapshot_json=runtime_snapshot),
        ):
            max_power_kw, resource_kind = service._extract_resource_maps(object(), run_id=77)  # type: ignore[arg-type]

        self.assertEqual(max_power_kw.get("battery1"), 5.2)
        self.assertEqual(max_power_kw.get("shaby"), 5.2)
        self.assertEqual(max_power_kw.get("ev1"), 11.0)
        self.assertEqual(max_power_kw.get("electric_vehicle1"), 11.0)
        self.assertEqual(max_power_kw.get("ev_wallbox_1"), 11.0)
        self.assertEqual(resource_kind.get("battery1"), "battery")
        self.assertEqual(resource_kind.get("shaby"), "battery")
        self.assertEqual(resource_kind.get("ev1"), "ev")


class OutputSignalApiTests(TestCase):
    def _build_app(self, service: OutputProjectionService) -> FastAPI:
        app = FastAPI()
        app.include_router(eos_output_signals_router)
        app.dependency_overrides[get_output_projection_service] = lambda: service
        app.dependency_overrides[get_db] = lambda: object()
        return app

    def test_get_outputs_bundle_and_fetch_tracking(self) -> None:
        service = OutputProjectionService(settings=Settings())
        store = _AccessStateStore()
        run = SimpleNamespace(id=901)
        runtime_snapshot = {
            "devices": {
                "batteries": [
                    {
                        "max_charge_power_w": 6000,
                        "device_id": "shaby",
                    }
                ]
            }
        }
        instructions = [
            _instruction(
                instruction_id=1,
                run_id=901,
                resource_id="battery1",
                mode="FORCED_CHARGE",
                factor=0.5,
            )
        ]

        with patch(
            "app.services.output_projection.get_latest_successful_run_with_plan",
            return_value=run,
        ), patch(
            "app.services.output_projection.list_plan_instructions_for_run",
            return_value=instructions,
        ), patch(
            "app.services.output_projection.get_run_input_snapshot",
            return_value=SimpleNamespace(runtime_config_snapshot_json=runtime_snapshot),
        ), patch(
            "app.services.output_projection.get_latest_power_samples",
            return_value=[],
        ), patch(
            "app.services.output_projection.list_output_signal_access_states",
            side_effect=store.list,
        ), patch(
            "app.services.output_projection.upsert_output_signal_access_state",
            side_effect=store.upsert,
        ):
            app = self._build_app(service)
            client = TestClient(app)

            first = client.get(
                "/eos/get/outputs",
                headers={"x-forwarded-for": "10.1.2.3, 10.1.2.4"},
            )
            self.assertEqual(first.status_code, 200)
            self.assertIn("text/plain", first.headers.get("content-type", ""))
            self.assertIn("battery1_target_power_kw:3.0", first.text)

            json_response = client.get(
                "/eos/get/outputs?format=json",
                headers={"x-forwarded-for": "10.1.2.3, 10.1.2.4"},
            )
            self.assertEqual(json_response.status_code, 200)
            payload = json_response.json()
            self.assertEqual(payload["central_http_path"], "/eos/get/outputs")
            self.assertEqual(payload["run_id"], 901)
            signals = payload["signals"]
            self.assertEqual(sorted(signals.keys()), ["battery1_target_power_kw"])

            resource_row = signals["battery1_target_power_kw"]
            self.assertEqual(resource_row["signal_key"], "battery1_target_power_kw")
            self.assertEqual(resource_row["unit"], "kW")
            self.assertAlmostEqual(float(resource_row["requested_power_kw"]), 3.0, places=6)
            self.assertEqual(resource_row["fetch_count"], 2)
            self.assertEqual(resource_row["last_fetch_client"], "10.1.2.3")

            listed = client.get("/api/eos/output-signals")
            self.assertEqual(listed.status_code, 200)
            listed_payload = listed.json()
            listed_signals = listed_payload["signals"]
            listed_resource = listed_signals["battery1_target_power_kw"]
            self.assertEqual(listed_resource["fetch_count"], 2)
            self.assertEqual(listed_resource["last_fetch_client"], "10.1.2.3")
            self.assertIsNotNone(listed_resource["last_fetch_ts"])

            second = client.get("/eos/get/outputs")
            self.assertEqual(second.status_code, 200)
            self.assertIn("battery1_target_power_kw:3.0", second.text)

            second_json = client.get("/eos/get/outputs?format=json")
            self.assertEqual(second_json.status_code, 200)
            second_payload = second_json.json()
            second_signals = second_payload["signals"]
            second_resource = second_signals["battery1_target_power_kw"]
            self.assertEqual(second_resource["fetch_count"], 4)

            listed_again = client.get("/api/eos/output-signals")
            self.assertEqual(listed_again.status_code, 200)
            listed_again_payload = listed_again.json()
            listed_again_resource = listed_again_payload["signals"]["battery1_target_power_kw"]
            self.assertEqual(listed_again_resource["fetch_count"], 4)

    def test_multiple_batteries_expose_only_resource_keys(self) -> None:
        service = OutputProjectionService(settings=Settings())
        store = _AccessStateStore()
        run = SimpleNamespace(id=902)
        runtime_snapshot = {
            "devices": {
                "batteries": [
                    {"max_charge_power_w": 5000, "device_id": "battery_a"},
                    {"max_charge_power_w": 7000, "device_id": "battery_b"},
                ]
            }
        }
        instructions = [
            _instruction(
                instruction_id=1,
                run_id=902,
                resource_id="battery1",
                mode="FORCED_CHARGE",
                factor=1.0,
            ),
            _instruction(
                instruction_id=2,
                run_id=902,
                resource_id="battery2",
                mode="FORCED_DISCHARGE",
                factor=0.6,
            ),
        ]

        with patch(
            "app.services.output_projection.get_latest_successful_run_with_plan",
            return_value=run,
        ), patch(
            "app.services.output_projection.list_plan_instructions_for_run",
            return_value=instructions,
        ), patch(
            "app.services.output_projection.get_run_input_snapshot",
            return_value=SimpleNamespace(runtime_config_snapshot_json=runtime_snapshot),
        ), patch(
            "app.services.output_projection.get_latest_power_samples",
            return_value=[],
        ), patch(
            "app.services.output_projection.list_output_signal_access_states",
            side_effect=store.list,
        ), patch(
            "app.services.output_projection.upsert_output_signal_access_state",
            side_effect=store.upsert,
        ):
            app = self._build_app(service)
            client = TestClient(app)
            response = client.get("/eos/get/outputs?format=json")
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            signal_keys = sorted(payload["signals"].keys())
            self.assertEqual(signal_keys, ["battery1_target_power_kw", "battery2_target_power_kw"])

    def test_single_signal_endpoint_removed(self) -> None:
        app = FastAPI()
        app.include_router(eos_output_signals_router)
        client = TestClient(app)
        response = client.get("/eos/get/signal/battery_target_power_kw")
        self.assertEqual(response.status_code, 404)


class LegacyEndpointRemovalTests(TestCase):
    def test_legacy_dispatch_and_target_endpoints_are_not_available(self) -> None:
        app = FastAPI()
        app.include_router(eos_runtime_router)
        client = TestClient(app)

        self.assertEqual(client.get("/api/eos/outputs/events").status_code, 404)
        self.assertEqual(client.post("/api/eos/outputs/dispatch/force").status_code, 404)
        self.assertEqual(client.get("/api/eos/output-targets").status_code, 404)
        self.assertEqual(client.post("/api/eos/output-targets", json={}).status_code, 404)
        self.assertEqual(client.put("/api/eos/output-targets/1", json={}).status_code, 404)
