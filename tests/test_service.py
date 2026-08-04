"""Tests for standalone service configuration and snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from custom_components.polish_energy_price.tariff import WARSAW
from service.config import load_config
from service.runtime import (
    MqttPublisher,
    PriceService,
    ProfileRuntime,
    _handler_for,
    _hour_key,
)


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

    def test_profile_forecast_contains_48_slots_without_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._service_config(directory, profiles=1)
            runtime = ProfileRuntime(config.profiles[0], Path(directory))
            result = runtime.forecast(
                datetime(2026, 8, 4, 12, 15, tzinfo=WARSAW)
            )
        self.assertEqual(48, len(result.slots))
        self.assertTrue(result.complete)
        self.assertEqual("bundled", result.energy_source)

    def test_service_keeps_coherent_forecasts_for_multiple_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PriceService(self._service_config(directory, profiles=2))
            service.recalculate(datetime(2026, 8, 4, 12, 15, tzinfo=WARSAW))
            forecasts = service.forecasts(24)
        self.assertEqual({"dom", "domek"}, set(forecasts))
        self.assertTrue(all(len(item.slots) == 24 for item in forecasts.values()))
        self.assertTrue(all(item.requested_hours == 24 for item in forecasts.values()))

    def test_forecast_http_endpoints_and_hours_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PriceService(self._service_config(directory, profiles=2))
            service.recalculate(datetime(2026, 8, 4, 12, 15, tzinfo=WARSAW))
            server, thread, base_url = self._http_server(service)
            try:
                with urlopen(f"{base_url}/api/forecast/dom?hours=2") as response:
                    single = json.load(response)
                with urlopen(f"{base_url}/api/forecast?hours=3") as response:
                    multiple = json.load(response)
                self.assertEqual(2, len(single["slots"]))
                self.assertEqual(2, single["requested_hours"])
                self.assertEqual({"dom", "domek"}, set(multiple["profiles"]))
                self.assertTrue(
                    all(
                        len(item["slots"]) == 3
                        for item in multiple["profiles"].values()
                    )
                )
                for value in ("wrong", "0", "169"):
                    with self.subTest(hours=value):
                        with self.assertRaises(HTTPError) as raised:
                            urlopen(f"{base_url}/api/forecast/dom?hours={value}")
                        self.assertEqual(400, raised.exception.code)
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base_url}/api/forecast/missing")
                self.assertEqual(404, raised.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_mqtt_publishes_retained_forecast_with_configured_qos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PriceService(self._service_config(directory, profiles=1))
            service.recalculate(datetime(2026, 8, 4, 12, 15, tzinfo=WARSAW))
            publisher = MqttPublisher(
                service.config.mqtt,
                service.snapshots,
                service.forecasts,
            )
            publisher._client = MagicMock()
            publisher._connected = True
            publisher.publish_all()
        calls = [
            call
            for call in publisher._client.publish.call_args_list
            if call.args[0] == "polish_energy_prices/dom/forecast"
        ]
        self.assertEqual(1, len(calls))
        self.assertEqual(service.config.mqtt.qos, calls[0].kwargs["qos"])
        self.assertTrue(calls[0].kwargs["retain"])
        payload = json.loads(calls[0].args[1])
        self.assertEqual("polish_energy_price", payload["provider"])
        self.assertEqual("tauron:g11", payload["tariff_id"])
        self.assertEqual("PLN/kWh", payload["unit"])
        self.assertEqual(48, len(payload["slots"]))

    def test_mqtt_replaces_expired_forecast_with_an_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PriceService(self._service_config(directory, profiles=1))
            service.recalculate(datetime(2027, 1, 1, 0, 15, tzinfo=WARSAW))
            forecast = service.forecasts()["dom"]
        self.assertEqual((), forecast.slots)
        self.assertFalse(forecast.complete)
        self.assertEqual("expired", forecast.source_status)

    def test_hour_key_distinguishes_both_fall_dst_hours(self) -> None:
        first = datetime(2026, 10, 25, 2, 30, tzinfo=WARSAW, fold=0)
        second = datetime(2026, 10, 25, 2, 30, tzinfo=WARSAW, fold=1)
        self.assertEqual(timedelta(hours=1), second.utcoffset())
        self.assertNotEqual(_hour_key(first), _hour_key(second))

    @staticmethod
    def _service_config(directory: str, *, profiles: int):
        extra = ""
        if profiles == 2:
            extra = """
  domek:
    operator: pge
    tariff: G11
    price_source: custom
    custom_prices:
      calodobowa: 0.7500
"""
        content = f"""
profiles:
  dom:
    operator: tauron
    tariff: G11
    price_source: regulated
{extra}
mqtt:
  enabled: false
  qos: 2
  retain: false
data_dir: {directory}
"""
        path = Path(directory) / "service-test.yaml"
        path.write_text(content, encoding="utf-8")
        return load_config(path)

    @staticmethod
    def _http_server(service: PriceService):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(service))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        return server, thread, f"http://{host}:{port}"


if __name__ == "__main__":
    unittest.main()
