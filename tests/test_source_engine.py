"""Tests for the platform-independent source engine."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import unittest

from custom_components.polish_energy_price.source_engine import (
    EnergyPriceSourceEngine,
)
from custom_components.polish_energy_price.tariff import WARSAW


class SourceEngineTests(unittest.TestCase):
    def test_round_trips_valid_cache(self) -> None:
        engine = EnergyPriceSourceEngine("tauron", "G11", "regulated")
        original = engine.initial_data()
        restored = engine.data_from_cache(engine.cache_payload(original))
        self.assertEqual(original.prices, restored.prices)
        self.assertEqual(original.distribution_net, restored.distribution_net)
        self.assertEqual(original.system_net, restored.system_net)

    def test_rejects_cache_for_another_tariff(self) -> None:
        engine = EnergyPriceSourceEngine("tauron", "G11", "regulated")
        payload = engine.cache_payload(engine.initial_data())
        payload["group"] = "G12"
        with self.assertRaisesRegex(ValueError, "innej taryfy"):
            engine.data_from_cache(payload)

    def test_source_failure_retains_bundled_prices(self) -> None:
        engine = EnergyPriceSourceEngine("tauron", "G11", "regulated")
        original = engine.initial_data()

        async def fetch_failure(*_args, **_kwargs):
            raise TimeoutError("brak odpowiedzi")

        async def run_sync(function, *args):
            return function(*args)

        refreshed = asyncio.run(
            engine.refresh(
                original,
                fetch_failure,
                run_sync,
                datetime(2026, 7, 31, 12, tzinfo=WARSAW),
            )
        )
        self.assertEqual(original.prices, refreshed.prices)
        self.assertEqual("bundled", refreshed.source)
        self.assertIn("brak odpowiedzi", refreshed.error or "")
        self.assertIn("brak odpowiedzi", refreshed.official_error or "")

    def test_dynamic_days_are_fetched_once_after_publication(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        responses = [
            self._kompas_payload("2026-08-04 10:00", 1),
            self._kompas_payload("2026-08-05 10:00", 2),
        ]
        calls = 0

        async def fetch(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return responses[calls - 1]

        first = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), fetch, now)
        )
        second = asyncio.run(
            engine._refresh_dynamic(
                first,
                datetime(2026, 8, 4, 13, tzinfo=WARSAW).isoformat(),
                fetch,
                datetime(2026, 8, 4, 13, tzinfo=WARSAW),
            )
        )

        self.assertEqual(2, calls)
        self.assertEqual(first.dynamic_zones, second.dynamic_zones)

    def test_dynamic_day_is_retried_until_it_is_published(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        first_now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        responses = [
            self._kompas_payload("2026-08-04 10:00", 1),
            json.dumps({"value": []}).encode(),
            self._kompas_payload("2026-08-05 10:00", 2),
        ]
        calls = 0

        async def fetch(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return responses[calls - 1]

        first = asyncio.run(
            engine._refresh_dynamic(
                current, first_now.isoformat(), fetch, first_now
            )
        )
        second_now = datetime(2026, 8, 4, 13, tzinfo=WARSAW)
        second = asyncio.run(
            engine._refresh_dynamic(
                first, second_now.isoformat(), fetch, second_now
            )
        )

        self.assertEqual(3, calls)
        self.assertEqual(2, len(second.dynamic_zones or {}))

    @staticmethod
    def _kompas_payload(utc_hour: str, level: int) -> bytes:
        return json.dumps(
            {
                "value": [
                    {
                        "dtime_utc": utc_hour,
                        "is_active": True,
                        "usage_fcst": level,
                        "publication_ts_utc": "2026-08-03 15:00:00",
                    }
                ]
            }
        ).encode()


if __name__ == "__main__":
    unittest.main()
