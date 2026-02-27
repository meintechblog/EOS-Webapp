from __future__ import annotations

from contextlib import contextmanager
from unittest import TestCase

from app.core.config import Settings
from app.schemas.setup_fields import SetupEntityMutateRequest
from app.services.setup_fields import (
    SetupFieldService,
    _battery_index_for_device_id_field,
    _home_appliance_window_duration_to_ui,
    _home_appliance_window_start_to_ui,
    _normalize_home_appliance_window_duration,
    _normalize_home_appliance_window_start,
    _param_path_to_field_id,
    _sync_inverter_battery_references,
)


class _DummyParameterCatalogService:
    def build_catalog(self) -> dict[str, object]:
        return {
            "provider_options": {
                "pvforecast.provider": [],
                "elecprice.provider": [],
                "feedintariff.provider": [],
                "load.provider": [],
            },
            "bidding_zone_options": [],
        }


class _DummyParameterProfileService:
    pass


@contextmanager
def _unused_session_factory_context():
    yield None


class _UnusedSessionFactory:
    def __call__(self):
        return _unused_session_factory_context()


def _build_service() -> SetupFieldService:
    return SetupFieldService(
        settings=Settings(),
        session_factory=_UnusedSessionFactory(),
        parameter_profile_service=_DummyParameterProfileService(),  # type: ignore[arg-type]
        parameter_catalog_service=_DummyParameterCatalogService(),  # type: ignore[arg-type]
        emr_pipeline_service=None,
    )


def _base_payload() -> dict[str, object]:
    return {
        "pvforecast": {
            "provider": "PVForecastImport",
            "planes": [
                {
                    "peakpower": 30,
                    "surface_azimuth": 180,
                    "surface_tilt": 10,
                    "inverter_paco": 30000,
                }
            ],
            "max_planes": 1,
        },
        "devices": {
            "electric_vehicles": [
                {
                    "device_id": "ev-main",
                    "capacity_wh": 70000,
                    "min_charge_power_w": 4000,
                    "max_charge_power_w": 11000,
                    "min_soc_percentage": 0,
                    "max_soc_percentage": 80,
                    "charging_efficiency": 0.9,
                    "discharging_efficiency": 1.0,
                }
            ],
            "home_appliances": [],
            "max_electric_vehicles": 1,
            "max_home_appliances": 0,
        },
    }


class SetupEntityMutationTests(TestCase):
    def setUp(self) -> None:
        self.service = _build_service()

    def test_add_ev_via_clone(self) -> None:
        payload = _base_payload()

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(
                action="add",
                entity_type="electric_vehicle",
                clone_from_item_key="electric_vehicle:0",
            ),
            warnings=[],
        )

        devices = payload["devices"]
        self.assertIsInstance(devices, dict)
        electric_vehicles = devices["electric_vehicles"]
        self.assertIsInstance(electric_vehicles, list)
        self.assertEqual(len(electric_vehicles), 2)
        source = electric_vehicles[0]
        added = electric_vehicles[1]
        self.assertIsInstance(source, dict)
        self.assertIsInstance(added, dict)
        self.assertEqual(source["capacity_wh"], added["capacity_wh"])
        self.assertNotEqual(source["device_id"], added["device_id"])
        self.assertTrue(str(added["device_id"]).startswith("ev"))
        self.assertEqual(devices["max_electric_vehicles"], 2)

    def test_add_ev_without_source_uses_template(self) -> None:
        payload = _base_payload()
        devices = payload["devices"]
        self.assertIsInstance(devices, dict)
        devices["electric_vehicles"] = []
        devices["max_electric_vehicles"] = 0
        warnings: list[str] = []

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(action="add", entity_type="electric_vehicle"),
            warnings=warnings,
        )

        electric_vehicles = devices["electric_vehicles"]
        self.assertIsInstance(electric_vehicles, list)
        self.assertEqual(len(electric_vehicles), 1)
        self.assertIsInstance(electric_vehicles[0], dict)
        self.assertEqual(electric_vehicles[0]["device_id"], "ev1")
        self.assertEqual(devices["max_electric_vehicles"], 1)
        self.assertTrue(any("template fallback" in message.lower() for message in warnings))

    def test_remove_ev_success(self) -> None:
        payload = _base_payload()
        devices = payload["devices"]
        self.assertIsInstance(devices, dict)
        electric_vehicles = devices["electric_vehicles"]
        self.assertIsInstance(electric_vehicles, list)
        electric_vehicles.append(
            {
                "device_id": "ev2",
                "capacity_wh": 55000,
                "min_charge_power_w": 2000,
                "max_charge_power_w": 7000,
                "min_soc_percentage": 5,
                "max_soc_percentage": 90,
            }
        )

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(
                action="remove",
                entity_type="electric_vehicle",
                item_key="electric_vehicle:0",
            ),
            warnings=[],
        )

        self.assertEqual(len(electric_vehicles), 1)
        self.assertIsInstance(electric_vehicles[0], dict)
        self.assertEqual(electric_vehicles[0]["device_id"], "ev2")
        self.assertEqual(devices["max_electric_vehicles"], 1)

    def test_remove_pv_plane_zero_is_blocked(self) -> None:
        payload = _base_payload()

        with self.assertRaises(ValueError):
            self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
                payload=payload,
                request=SetupEntityMutateRequest(
                    action="remove",
                    entity_type="pv_plane",
                    item_key="pv_plane:0",
                ),
                warnings=[],
            )

    def test_add_and_remove_pv_plane_syncs_max_planes(self) -> None:
        payload = _base_payload()
        pvforecast = payload["pvforecast"]
        self.assertIsInstance(pvforecast, dict)

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(action="add", entity_type="pv_plane"),
            warnings=[],
        )
        self.assertIsInstance(pvforecast["planes"], list)
        self.assertEqual(len(pvforecast["planes"]), 2)
        self.assertEqual(pvforecast["max_planes"], 2)

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(
                action="remove",
                entity_type="pv_plane",
                item_key="pv_plane:1",
            ),
            warnings=[],
        )
        self.assertEqual(len(pvforecast["planes"]), 1)
        self.assertEqual(pvforecast["max_planes"], 1)

    def test_battery_device_id_field_index_parser(self) -> None:
        self.assertEqual(_battery_index_for_device_id_field("param.devices.batteries.0.device_id"), 0)
        self.assertEqual(_battery_index_for_device_id_field("param.devices.batteries.12.device_id"), 12)
        self.assertIsNone(_battery_index_for_device_id_field("param.devices.batteries.0.capacity_wh"))

    def test_sync_inverter_battery_references_single_battery_updates_all(self) -> None:
        payload = {
            "devices": {
                "batteries": [
                    {"device_id": "lfp"},
                ],
                "inverters": [
                    {"battery_id": "lfp"},
                    {"battery_id": "legacy"},
                ],
            }
        }

        updated = _sync_inverter_battery_references(
            payload=payload,
            battery_index=0,
            old_battery_id="lfp",
            new_battery_id="hausspeicher",
        )

        self.assertEqual(updated, [0, 1])
        inverters = payload["devices"]["inverters"]
        self.assertEqual(inverters[0]["battery_id"], "hausspeicher")
        self.assertEqual(inverters[1]["battery_id"], "hausspeicher")

    def test_sync_inverter_battery_references_multi_battery_updates_matching_only(self) -> None:
        payload = {
            "devices": {
                "batteries": [
                    {"device_id": "lfp"},
                    {"device_id": "speicher2"},
                ],
                "inverters": [
                    {"battery_id": "lfp"},
                    {"battery_id": "speicher2"},
                    {"battery_id": "extern"},
                ],
            }
        }

        updated = _sync_inverter_battery_references(
            payload=payload,
            battery_index=0,
            old_battery_id="lfp",
            new_battery_id="hausspeicher",
        )

        self.assertEqual(updated, [0])
        inverters = payload["devices"]["inverters"]
        self.assertEqual(inverters[0]["battery_id"], "hausspeicher")
        self.assertEqual(inverters[1]["battery_id"], "speicher2")
        self.assertEqual(inverters[2]["battery_id"], "extern")


class SetupFieldSafeHorizonCapTests(TestCase):
    def setUp(self) -> None:
        self.service = _build_service()

    def test_inverter_battery_id_options_use_battery_device_ids(self) -> None:
        payload = {
            **_base_payload(),
            "devices": {
                **_base_payload()["devices"],  # type: ignore[index]
                "batteries": [
                    {"device_id": "lfp"},
                    {"device_id": "speicher2"},
                    {"device_id": "lfp"},
                ],
                "inverters": [
                    {"device_id": "inv1", "battery_id": "lfp", "max_power_w": 30000},
                ],
            },
        }

        field_defs = self.service._field_defs(payload)  # type: ignore[attr-defined]
        inverter_battery_field = next(
            field for field in field_defs if field.field_id == "param.devices.inverters.0.battery_id"
        )
        options = self.service._resolve_options(inverter_battery_field, payload=payload)  # type: ignore[attr-defined]
        self.assertEqual(options, ["lfp", "speicher2"])

    def test_add_appliance_and_mutate_windows(self) -> None:
        payload = _base_payload()
        devices = payload["devices"]
        self.assertIsInstance(devices, dict)

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(action="add", entity_type="home_appliance"),
            warnings=[],
        )

        home_appliances = devices["home_appliances"]
        self.assertIsInstance(home_appliances, list)
        self.assertEqual(len(home_appliances), 1)
        self.assertEqual(devices["max_home_appliances"], 1)
        self.assertIsInstance(home_appliances[0], dict)
        windows = home_appliances[0]["time_windows"]["windows"]  # type: ignore[index]
        self.assertIsInstance(windows, list)
        self.assertEqual(len(windows), 1)

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(
                action="add",
                entity_type="home_appliance_window",
                parent_item_key="home_appliance:0",
            ),
            warnings=[],
        )
        self.assertEqual(len(windows), 2)

        self.service._mutate_payload_for_request(  # type: ignore[attr-defined]
            payload=payload,
            request=SetupEntityMutateRequest(
                action="remove",
                entity_type="home_appliance_window",
                item_key="home_appliance:0:window:0",
            ),
            warnings=[],
        )
        self.assertEqual(len(windows), 1)

    def test_home_appliance_window_conversion(self) -> None:
        normalized_start, start_error = _normalize_home_appliance_window_start("05:30")
        self.assertIsNone(start_error)
        self.assertEqual(normalized_start, "05:30:00.000000 UTC")
        self.assertEqual(_home_appliance_window_start_to_ui("05:30:00.000000 UTC"), "05:30")

        normalized_duration, duration_error = _normalize_home_appliance_window_duration(2.5)
        self.assertIsNone(duration_error)
        self.assertEqual(normalized_duration, "2.5 hours")
        self.assertEqual(_home_appliance_window_duration_to_ui("3 hours"), 3.0)

    def test_dynamic_param_path_mapping(self) -> None:
        payload = _base_payload()
        pvforecast = payload["pvforecast"]
        self.assertIsInstance(pvforecast, dict)
        self.assertIsInstance(pvforecast["planes"], list)
        pvforecast["planes"].append(
            {
                "peakpower": 5,
                "surface_azimuth": 90,
                "surface_tilt": 20,
                "inverter_paco": 5000,
            }
        )

        devices = payload["devices"]
        self.assertIsInstance(devices, dict)
        devices["electric_vehicles"] = [
            {
                "device_id": "ev1",
                "capacity_wh": 50000,
                "min_charge_power_w": 1100,
                "max_charge_power_w": 7000,
                "min_soc_percentage": 10,
                "max_soc_percentage": 90,
            }
        ]
        devices["home_appliances"] = [
            {
                "device_id": "appliance1",
                "consumption_wh": 2000,
                "time_windows": {
                    "windows": [
                        {
                            "start_time": "08:00:00.000000 UTC",
                            "duration": "2 hours",
                        },
                        {
                            "start_time": "18:00:00.000000 UTC",
                            "duration": "3 hours",
                        },
                    ]
                },
            }
        ]

        field_id, scale = _param_path_to_field_id(
            param_path="pvforecast/planes/1/inverter_paco_kw",
            payload=payload,
        )
        self.assertEqual(field_id, "param.pvforecast.planes.1.inverter_paco")
        self.assertEqual(scale, 1.0)

        field_id, scale = _param_path_to_field_id(
            param_path="devices/electric_vehicles/ev1/capacity_kwh",
            payload=payload,
        )
        self.assertEqual(field_id, "param.devices.electric_vehicles.0.capacity_wh")
        self.assertEqual(scale, 1.0)

        field_id, scale = _param_path_to_field_id(
            param_path="devices/home_appliances/appliance1/consumption_kwh",
            payload=payload,
        )
        self.assertEqual(field_id, "param.devices.home_appliances.0.consumption_wh")
        self.assertEqual(scale, 1.0)

        field_id, scale = _param_path_to_field_id(
            param_path="devices/home_appliances/appliance1/time_windows/windows/1/duration_h",
            payload=payload,
        )
        self.assertEqual(field_id, "param.devices.home_appliances.0.time_windows.windows.1.duration_h")
        self.assertEqual(scale, 1.0)
