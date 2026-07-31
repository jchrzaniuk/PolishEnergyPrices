"""Tests for the platform-independent source engine."""

from __future__ import annotations

import asyncio
from datetime import datetime
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


if __name__ == "__main__":
    unittest.main()
