"""Unit tests for standalone service configuration validation."""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from service.config import _expand_environment, load_config
from service.runtime import PriceService


def write_config(directory: str, content: str) -> Path:
    """Store a YAML fixture and return its path."""

    path = Path(directory) / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class ConfigTests(unittest.TestCase):
    def test_load_config_selects_price_source_defaults(self) -> None:
        cases = (
            ("G11", "regulated"),
            ("G13s", "tauron_g13s"),
            ("G14dynamic", "tauron_g14dynamic"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for tariff, expected_source in cases:
                with self.subTest(tariff=tariff):
                    config = load_config(
                        write_config(
                            directory,
                            f"""
profiles:
  dom:
    operator: tauron
    tariff: {tariff}
mqtt:
  enabled: false
""",
                        )
                    )

                    profile = config.profiles[0]

                    self.assertEqual(expected_source, profile.price_source)
                    self.assertEqual(tariff.lower(), profile.tariff.lower())
                    self.assertFalse(config.mqtt.enabled)

    def test_load_config_preserves_explicit_settings_and_normalizes_empty_credentials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(
                write_config(
                    directory,
                    """
profiles:
  mieszkanie:
    operator: enea
    tariff: G12
    price_source: regulated
    meter_clock: fixed_winter_time
    day_hours: "6-13,15-22"
http:
  host: 127.0.0.1
  port: 8123
mqtt:
  host: broker.local
  port: 8883
  username: ""
  password: ""
  topic_prefix: /energy/prices/
  qos: 2
  retain: false
  tls: true
  ca_cert: /certs/ca.pem
refresh_interval_hours: 0.25
data_dir: /var/lib/energy
""",
                )
            )

        profile = config.profiles[0]

        self.assertEqual(
            ("mieszkanie", "enea", "G12"),
            (profile.profile_id, profile.operator, profile.tariff),
        )
        self.assertEqual(
            ("fixed_winter_time", "6-13,15-22"),
            (profile.meter_clock, profile.day_hours),
        )
        self.assertEqual(("127.0.0.1", 8123), (config.http.host, config.http.port))
        self.assertIsNone(config.mqtt.username)
        self.assertIsNone(config.mqtt.password)
        self.assertEqual(
            ("energy/prices", 2, False),
            (config.mqtt.topic_prefix, config.mqtt.qos, config.mqtt.retain),
        )
        self.assertEqual(
            (True, "/certs/ca.pem"),
            (config.mqtt.tls, config.mqtt.ca_cert),
        )
        self.assertEqual(
            (0.25, "/var/lib/energy"),
            (config.refresh_interval_hours, config.data_dir),
        )

    def test_expand_environment_recurses_through_mappings_and_lists(self) -> None:
        with patch.dict(
            os.environ,
            {"MQTT_HOST": "broker.internal", "MQTT_PASSWORD": "s3cret"},
        ):
            expanded = _expand_environment(
                {
                    "host": "${MQTT_HOST}",
                    "credentials": ["user", "${MQTT_PASSWORD}"],
                    "unchanged": 42,
                }
            )

        self.assertEqual(
            {
                "host": "broker.internal",
                "credentials": ["user", "s3cret"],
                "unchanged": 42,
            },
            expanded,
        )

    def test_load_config_rejects_invalid_values(self) -> None:
        cases = (
            (
                """
profiles:
  Invalid Profile:
    operator: tauron
    tariff: G11
mqtt:
  enabled: false
""",
                "Nieprawidłowy identyfikator",
            ),
            (
                """
profiles:
  dom:
    operator: tauron
    tariff: G11
http:
  port: 0
mqtt:
  enabled: false
""",
                "http.port",
            ),
            (
                """
profiles:
  dom:
    operator: tauron
    tariff: G11
mqtt:
  qos: 3
""",
                "mqtt.qos",
            ),
            (
                """
profiles:
  dom:
    operator: tauron
    tariff: G11
mqtt:
  host: ""
""",
                "mqtt.host",
            ),
            (
                """
profiles:
  dom:
    operator: tauron
    tariff: G11
mqtt:
  topic_prefix: ///
""",
                "mqtt.topic_prefix",
            ),
            (
                """
profiles:
  dom:
    operator: tauron
    tariff: G11
mqtt:
  enabled: false
refresh_interval_hours: 0.24
""",
                "refresh_interval_hours",
            ),
            (
                """
profiles:
  dom:
    operator: tauron
    tariff: G13s
    price_source: regulated
mqtt:
  enabled: false
""",
                "G13s nie występuje",
            ),
            (
                """
profiles:
  dom:
    operator: pge
    tariff: G11
    price_source: tauron_g14dynamic
mqtt:
  enabled: false
""",
                "tylko dla TAURON G14dynamic",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for content, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        load_config(write_config(directory, content))

    def test_load_config_accepts_port_qos_and_refresh_boundaries(self) -> None:
        cases = ((1, 0, 0.25), (65535, 2, 168))
        with tempfile.TemporaryDirectory() as directory:
            for port, qos, refresh_interval in cases:
                with self.subTest(
                    port=port, qos=qos, refresh_interval=refresh_interval
                ):
                    config = load_config(
                        write_config(
                            directory,
                            f"""
profiles:
  dom:
    operator: tauron
    tariff: G11
http:
  port: {port}
mqtt:
  qos: {qos}
refresh_interval_hours: {refresh_interval}
""",
                        )
                    )

                    self.assertEqual(port, config.http.port)
                    self.assertEqual(qos, config.mqtt.qos)
                    self.assertEqual(refresh_interval, config.refresh_interval_hours)

    def test_load_config_rejects_unresolved_environment_reference(self) -> None:
        content = """
profiles:
  dom:
    operator: tauron
    tariff: G11
mqtt:
  enabled: false
data_dir: ${UNSET_PRICE_DATA_DIR_FOR_TEST}
"""
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "Brak zmiennej środowiskowej"):
                load_config(write_config(directory, content))

    def test_export_settlement_and_correction_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(
                write_config(
                    directory,
                    """
profiles:
  dom:
    operator: tauron
    tariff: G11
mqtt:
  enabled: false
""",
                )
            )

        profile = config.profiles[0]

        self.assertEqual("off", profile.export_settlement)
        self.assertEqual(1.0, profile.export_correction)

    def test_export_settlement_and_correction_are_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(
                write_config(
                    directory,
                    """
profiles:
  dom:
    operator: tauron
    tariff: G11
    export_settlement: rce
    export_correction: "1,23"
mqtt:
  enabled: false
""",
                )
            )

        profile = config.profiles[0]

        self.assertEqual("rce", profile.export_settlement)
        self.assertEqual(1.23, profile.export_correction)

    def test_load_config_rejects_unknown_export_settlement(self) -> None:
        content = """
profiles:
  dom:
    operator: tauron
    tariff: G11
    export_settlement: monthly
mqtt:
  enabled: false
"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "export_settlement"):
                load_config(write_config(directory, content))

    def test_load_config_rejects_export_correction_out_of_range(self) -> None:
        content = """
profiles:
  dom:
    operator: tauron
    tariff: G11
    export_settlement: rce
    export_correction: 2.5
mqtt:
  enabled: false
"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "export_correction"):
                load_config(write_config(directory, content))

    def test_export_enabled_profile_forces_hourly_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(
                write_config(
                    directory,
                    f"""
profiles:
  dom:
    operator: tauron
    tariff: G11
    export_settlement: rce
mqtt:
  enabled: false
refresh_interval_hours: 12
data_dir: {directory}
""",
                )
            )
            service = PriceService(config)

            self.assertEqual(
                timedelta(hours=1).total_seconds(),
                service._profile_interval_seconds(service.profiles["dom"]),
            )


if __name__ == "__main__":
    unittest.main()
