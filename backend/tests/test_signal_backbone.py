from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import TestCase

from app.repositories.signal_backbone import _ingest_lag_ms


class SignalBackboneTests(TestCase):
    def test_ingest_lag_ms_clamps_to_int32_max(self) -> None:
        ingested_at = datetime(2026, 2, 21, 14, 0, tzinfo=timezone.utc)
        signal_ts = ingested_at - timedelta(days=40)

        lag_ms = _ingest_lag_ms(ingested_at, signal_ts)

        self.assertEqual(lag_ms, 2_147_483_647)

    def test_ingest_lag_ms_future_signal_is_zero(self) -> None:
        ingested_at = datetime(2026, 2, 21, 14, 0, tzinfo=timezone.utc)
        signal_ts = ingested_at + timedelta(minutes=5)

        lag_ms = _ingest_lag_ms(ingested_at, signal_ts)

        self.assertEqual(lag_ms, 0)
