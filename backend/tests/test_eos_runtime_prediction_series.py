from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase

from app.api.eos_runtime import (
    _extract_prediction_datetime_index_from_payload,
    _extract_prediction_numeric_map_from_payload,
    _resolve_series_value,
)


class EosRuntimePredictionSeriesTests(TestCase):
    def test_extract_datetime_index_from_data_map(self) -> None:
        payload = {
            "data": {
                "2026-02-21T14:15:00Z": "2026-02-21T14:15:00Z",
                "2026-02-21T14:00:00Z": "2026-02-21T14:00:00Z",
            }
        }

        index = _extract_prediction_datetime_index_from_payload(payload)

        self.assertEqual(
            index,
            [
                datetime(2026, 2, 21, 14, 0, tzinfo=timezone.utc),
                datetime(2026, 2, 21, 14, 15, tzinfo=timezone.utc),
            ],
        )

    def test_extract_numeric_map_from_data_map(self) -> None:
        payload = {
            "data": {
                "2026-02-21T14:00:00Z": 0.04933,
                "2026-02-21T14:15:00Z": 0.06023,
            }
        }

        values_by_ts = _extract_prediction_numeric_map_from_payload(payload)

        assert values_by_ts is not None
        self.assertEqual(values_by_ts[datetime(2026, 2, 21, 14, 0, tzinfo=timezone.utc)], 0.04933)
        self.assertEqual(values_by_ts[datetime(2026, 2, 21, 14, 15, tzinfo=timezone.utc)], 0.06023)

    def test_resolve_series_value_prefers_timestamp_map(self) -> None:
        ts = datetime(2026, 2, 21, 14, 0, tzinfo=timezone.utc)
        mapped = _resolve_series_value(
            values=[99.0],
            by_ts={ts: 1.23},
            ts=ts,
            index=0,
            factor=100.0,
        )
        self.assertEqual(mapped, 123.0)

    def test_resolve_series_value_falls_back_to_indexed_values(self) -> None:
        ts = datetime(2026, 2, 21, 14, 0, tzinfo=timezone.utc)
        fallback = _resolve_series_value(
            values=[1.5, 2.5],
            by_ts=None,
            ts=ts,
            index=1,
            factor=10.0,
        )
        self.assertEqual(fallback, 25.0)

    def test_resolve_series_value_uses_step_hold_for_misaligned_series(self) -> None:
        by_ts = {
            datetime(2026, 2, 21, 10, 0, tzinfo=timezone.utc): 100.0,
            datetime(2026, 2, 21, 11, 0, tzinfo=timezone.utc): 110.0,
            datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc): 120.0,
        }
        ts = datetime(2026, 2, 21, 11, 45, tzinfo=timezone.utc)
        resolved = _resolve_series_value(
            values=None,
            by_ts=by_ts,
            ts=ts,
            index=0,
            factor=1.0,
        )
        self.assertEqual(resolved, 110.0)

    def test_resolve_series_value_uses_start_fallback_for_nearby_first_point(self) -> None:
        by_ts = {
            datetime(2026, 2, 21, 10, 0, tzinfo=timezone.utc): 100.0,
            datetime(2026, 2, 21, 11, 0, tzinfo=timezone.utc): 110.0,
            datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc): 120.0,
        }
        ts = datetime(2026, 2, 21, 9, 45, tzinfo=timezone.utc)
        resolved = _resolve_series_value(
            values=None,
            by_ts=by_ts,
            ts=ts,
            index=0,
            factor=1.0,
        )
        self.assertEqual(resolved, 100.0)

    def test_resolve_series_value_does_not_fill_large_gap(self) -> None:
        by_ts = {
            datetime(2026, 2, 21, 10, 0, tzinfo=timezone.utc): 100.0,
            datetime(2026, 2, 21, 11, 0, tzinfo=timezone.utc): 110.0,
            datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc): 120.0,
            datetime(2026, 2, 21, 18, 0, tzinfo=timezone.utc): 180.0,
        }
        ts = datetime(2026, 2, 21, 14, 45, tzinfo=timezone.utc)
        resolved = _resolve_series_value(
            values=None,
            by_ts=by_ts,
            ts=ts,
            index=0,
            factor=1.0,
        )
        self.assertIsNone(resolved)

    def test_resolve_series_value_prefers_exact_match_over_step_hold(self) -> None:
        by_ts = {
            datetime(2026, 2, 21, 10, 0, tzinfo=timezone.utc): 100.0,
            datetime(2026, 2, 21, 11, 0, tzinfo=timezone.utc): 110.0,
            datetime(2026, 2, 21, 12, 0, tzinfo=timezone.utc): 120.0,
        }
        ts = datetime(2026, 2, 21, 11, 0, tzinfo=timezone.utc)
        resolved = _resolve_series_value(
            values=[999.0],
            by_ts=by_ts,
            ts=ts,
            index=0,
            factor=1.0,
        )
        self.assertEqual(resolved, 110.0)
