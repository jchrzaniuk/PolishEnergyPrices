"""Regression tests for all bundled 2026 tariffs (no Home Assistant required)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "polish_energy_price"
    / "tariff.py"
)
SPEC = importlib.util.spec_from_file_location("polish_energy_tariff", MODULE_PATH)
assert SPEC and SPEC.loader
tariff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tariff
SPEC.loader.exec_module(tariff)


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TariffTests(unittest.TestCase):
    def test_all_20_static_tariffs_cover_every_hour_of_2026(self) -> None:
        self.assertEqual(21, len(tariff.TARIFFS))
        static_tariffs = [
            item
            for item in tariff.TARIFFS.values()
            if item.dynamic_zone_source is None
        ]
        self.assertEqual(20, len(static_tariffs))
        instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2027, 1, 1, tzinfo=timezone.utc)
        while instant < end:
            for definition in static_tariffs:
                result = tariff.price_at(definition, instant)
                self.assertIn(result.zone_key, definition.zones)
                self.assertGreater(result.total, 0)
                self.assertEqual(
                    result.total, round(result.energy + result.distribution, 4)
                )
            instant += timedelta(hours=1)

    def test_tauron_g13_winter_prices_and_weekend(self) -> None:
        definition = tariff.get_tariff("tauron", "G13")
        off = tariff.price_at(definition, at("2026-01-15T03:00:00+01:00"))
        morning = tariff.price_at(definition, at("2026-01-15T08:00:00+01:00"))
        afternoon = tariff.price_at(definition, at("2026-01-15T17:00:00+01:00"))
        weekend = tariff.price_at(definition, at("2026-01-17T17:00:00+01:00"))
        self.assertEqual("pozostale", off.zone_key)
        self.assertEqual("szczyt_przedpoludniowy", morning.zone_key)
        self.assertEqual("szczyt_popoludniowy", afternoon.zone_key)
        self.assertEqual("pozostale", weekend.zone_key)
        self.assertEqual(
            (0.6257, 0.9048, 1.4961), (off.total, morning.total, afternoon.total)
        )

    def test_explicit_zone_prices_match_tauron_g13_meter_registers(self) -> None:
        definition = tariff.get_tariff("tauron", "G13")
        self.assertEqual(
            {
                "szczyt_przedpoludniowy": 0.9048,
                "szczyt_popoludniowy": 1.4961,
                "pozostale": 0.6257,
            },
            {
                zone: tariff.price_for_zone(definition, zone).total
                for zone in definition.zones
            },
        )

    def test_tauron_g13s_uses_season_day_type_and_hour(self) -> None:
        definition = tariff.get_tariff("tauron", "g13s")
        cases = {
            "2026-01-15T08:00:00+01:00": (
                "zima_dzien_roboczy_dzienna_szczytowa",
                0.3332,
            ),
            "2026-01-15T12:00:00+01:00": (
                "zima_dzien_roboczy_dzienna_pozaszczytowa",
                0.1999,
            ),
            "2026-01-17T12:00:00+01:00": (
                "zima_dzien_wolny_dzienna_pozaszczytowa",
                0.1200,
            ),
            "2026-07-15T10:00:00+02:00": (
                "lato_dzien_roboczy_dzienna_pozaszczytowa",
                0.1000,
            ),
            "2026-07-19T18:00:00+02:00": (
                "lato_dzien_wolny_dzienna_szczytowa",
                0.1176,
            ),
            "2026-07-19T23:00:00+02:00": (
                "lato_dzien_wolny_nocna",
                0.1094,
            ),
        }
        for timestamp, (zone, network_rate) in cases.items():
            with self.subTest(timestamp=timestamp):
                result = tariff.price_at(definition, at(timestamp))
                self.assertEqual(zone, result.zone_key)
                self.assertEqual(network_rate, definition.distribution_net[zone])

    def test_tauron_g13s_treats_holiday_as_free_day(self) -> None:
        definition = tariff.get_tariff("tauron", "G13s")
        self.assertEqual(
            "zima_dzien_wolny_dzienna_pozaszczytowa",
            tariff.zone_at(definition, at("2026-12-24T12:00:00+01:00")),
        )
        self.assertTrue(definition.external_statistics_supported)

    def test_tauron_g14dynamic_uses_an_absolute_kompas_hour(self) -> None:
        definition = tariff.get_tariff("tauron", "g14dynamic")
        instant = at("2026-08-04T12:30:00+02:00")
        key = tariff.dynamic_hour_key(instant)
        result = tariff.price_at(
            definition,
            instant,
            dynamic_zones={key: "S1_zalecane_uzytkowanie"},
        )
        self.assertEqual("S1_zalecane_uzytkowanie", result.zone_key)
        self.assertEqual(0.6175, result.energy)
        self.assertEqual(0.6986, result.total)
        self.assertFalse(definition.external_statistics_supported)

    def test_tauron_g14dynamic_refuses_to_guess_a_missing_zone(self) -> None:
        definition = tariff.get_tariff("tauron", "G14dynamic")
        with self.assertRaisesRegex(tariff.DynamicZoneUnavailable, "Brak strefy"):
            tariff.price_at(
                definition,
                at("2026-08-04T12:30:00+02:00"),
                dynamic_zones={},
            )

    def test_pge_g12_changes_hours_between_seasons(self) -> None:
        definition = tariff.get_tariff("pge", "G12")
        self.assertEqual(
            "nocna", tariff.zone_at(definition, at("2026-07-15T16:00:00+02:00"))
        )
        self.assertEqual(
            "dzienna", tariff.zone_at(definition, at("2026-01-15T16:00:00+01:00"))
        )

    def test_pge_g12n_saturday_is_not_an_all_day_night_zone(self) -> None:
        definition = tariff.get_tariff("pge", "G12n")
        self.assertEqual(
            "dzienna", tariff.zone_at(definition, at("2026-01-17T08:00:00+01:00"))
        )
        self.assertEqual(
            "nocna", tariff.zone_at(definition, at("2026-01-18T08:00:00+01:00"))
        )

    def test_enea_g12_accepts_contract_hour_override(self) -> None:
        definition = tariff.get_tariff("enea", "G12")
        instant = at("2026-01-15T14:00:00+01:00")
        self.assertEqual("nocna", tariff.zone_at(definition, instant))
        self.assertEqual(
            "dzienna", tariff.zone_at(definition, instant, day_hours="6-21")
        )

    def test_fixed_winter_clock_shifts_summer_zone_by_one_hour(self) -> None:
        definition = tariff.get_tariff("tauron", "G12")
        instant = at("2026-07-15T06:30:00+02:00")
        self.assertEqual("dzienna", tariff.zone_at(definition, instant))
        self.assertEqual(
            "nocna", tariff.zone_at(definition, instant, fixed_winter_time=True)
        )

    def test_christmas_eve_is_a_statutory_off_peak_day(self) -> None:
        self.assertIn(datetime(2026, 12, 24).date(), tariff.polish_holidays(2026))
        definition = tariff.get_tariff("tauron", "G12w")
        self.assertEqual(
            "pozaszczytowa",
            tariff.zone_at(definition, at("2026-12-24T10:00:00+01:00")),
        )

    def test_custom_energy_prices_replace_only_energy_component(self) -> None:
        definition = tariff.get_tariff("stoen", "G11")
        instant = at("2026-01-15T12:00:00+01:00")
        regulated = tariff.price_at(definition, instant)
        custom = tariff.price_at(definition, instant, custom_energy={"calodobowa": 0.8})
        self.assertEqual(regulated.distribution, custom.distribution)
        self.assertEqual(0.8, custom.energy)
        self.assertEqual(round(0.8 + custom.distribution, 4), custom.total)

    def test_excise_is_included_once_and_exposed_explicitly(self) -> None:
        definition = tariff.get_tariff("tauron", "G11")
        result = tariff.price_at(definition, at("2026-01-15T12:00:00+01:00"))
        self.assertEqual(0.6175, result.energy)
        self.assertEqual(0.0062, result.excise)
        self.assertEqual(
            0.6175,
            round((0.4970 + tariff.EXCISE_NET_PLN_KWH) * tariff.VAT, 4),
        )

    def test_invalid_hour_overlaps_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "nachodzić"):
            tariff.parse_day_hours("6-13,12-22")
        with self.assertRaisesRegex(ValueError, "formatu"):
            tariff.parse_day_hours("six to thirteen")


if __name__ == "__main__":
    unittest.main()
