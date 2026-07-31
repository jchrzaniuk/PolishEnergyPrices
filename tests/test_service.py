"""Tests for standalone service configuration and snapshots."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from custom_components.polish_energy_price.tariff import WARSAW
from service.config import load_config
from service.runtime import ProfileRuntime


class ServiceTests(unittest.TestCase):
    def test_loads_environment_credentials_and_multiple_profiles(self) -> None:
        content = """
profiles:
  dom:
    operator: tauron
    tariff: G11
    price_source: regulated
  domek:
    operator: pge
    tariff: G11
    price_source: custom
    custom_prices:
      calodobowa: 0.75
http:
  port: 8181
mqtt:
  host: broker
  username: ${TEST_MQTT_USER}
  password: ${TEST_MQTT_PASSWORD}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(content, encoding="utf-8")
            with patch.dict(
                os.environ,
                {"TEST_MQTT_USER": "użytkownik", "TEST_MQTT_PASSWORD": "hasło"},
            ):
                config = load_config(path)
        self.assertEqual(2, len(config.profiles))
        self.assertEqual(8181, config.http.port)
        self.assertEqual("użytkownik", config.mqtt.username)
        self.assertEqual("hasło", config.mqtt.password)

    def test_rejects_incomplete_custom_prices(self) -> None:
        content = """
profiles:
  dom:
    operator: tauron
    tariff: G12
    price_source: custom
    custom_prices:
      dzienna: 0.75
mqtt:
  enabled: false
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Niepełny zestaw"):
                load_config(path)

    def test_custom_profile_snapshot_contains_gross_components(self) -> None:
        content = """
profiles:
  dom:
    operator: tauron
    tariff: G11
    price_source: custom
    custom_prices:
      calodobowa: 0.7500
mqtt:
  enabled: false
data_dir: DATA_DIR
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                content.replace("DATA_DIR", directory), encoding="utf-8"
            )
            config = load_config(path)
            runtime = ProfileRuntime(config.profiles[0], Path(directory))
            result = runtime.snapshot(
                datetime(2026, 7, 31, 12, tzinfo=WARSAW)
            )
        self.assertTrue(result["available"])
        self.assertEqual("PLN/kWh", result["unit"])
        self.assertEqual("custom", result["energy_source"])
        self.assertEqual(0.75, result["energy_gross"])
        self.assertAlmostEqual(
            result["price_gross"],
            result["energy_gross"] + result["distribution_gross"],
            places=4,
        )


if __name__ == "__main__":
    unittest.main()
