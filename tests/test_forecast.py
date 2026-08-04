"""Tests for the platform-independent hourly forecast generator."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from custom_components.polish_energy_price.forecast import (
    build_forecast,
    forecast_attributes,
    forecast_to_dict,
    resize_forecast,
)
from custom_components.polish_energy_price.source_engine import EnergyPriceData
from custom_components.polish_energy_price.tariff import (
    WARSAW,
    dynamic_hour_key,
    get_tariff,
)


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def data_for(operator: str, group: str) -> EnergyPriceData:
    tariff = get_tariff(operator, group)
    return EnergyPriceData(
        prices=dict(tariff.energy_gross),
        source="ure",
        distribution_net=dict(tariff.distribution_net),
        system_net={
            "quality": 0.0332,
            "oze": 0.0073,
            "cogeneration": 0.0030,
        },
    )


class ForecastTests(unittest.TestCase):
    def test_builds_48_continuous_absolute_hours(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G12"),
            data_for("tauron", "G12"),
            at("2026-08-04T12:15:27+02:00"),
        )
        self.assertEqual(48, len(forecast.slots))
        self.assertTrue(forecast.complete)
        self.assertEqual(48, forecast.requested_hours)
        self.assertEqual(
            at("2026-08-04T12:00:00+02:00"), forecast.slots[0].start
        )
        starts_utc = [slot.start.astimezone(timezone.utc) for slot in forecast.slots]
        self.assertEqual(len(starts_utc), len(set(starts_utc)))
        for current, following in zip(forecast.slots, forecast.slots[1:]):
            self.assertEqual(
                current.end.astimezone(timezone.utc),
                following.start.astimezone(timezone.utc),
            )

    def test_crosses_tauron_g12_zone_boundary(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G12"),
            data_for("tauron", "G12"),
            at("2026-08-04T12:15:00+02:00"),
            hours=2,
        )
        self.assertEqual(["dzienna", "nocna"], [slot.zone for slot in forecast.slots])

    def test_weekend_and_holiday_use_the_off_peak_zone(self) -> None:
        tariff = get_tariff("tauron", "G12w")
        data = data_for("tauron", "G12w")
        weekend = build_forecast(
            tariff, data, at("2026-08-08T10:15:00+02:00"), hours=1
        )
        holiday = build_forecast(
            tariff, data, at("2026-12-24T10:15:00+01:00"), hours=1
        )
        self.assertEqual("pozaszczytowa", weekend.slots[0].zone)
        self.assertEqual("pozaszczytowa", holiday.slots[0].zone)

    def test_crosses_pge_season_boundary(self) -> None:
        forecast = build_forecast(
            get_tariff("pge", "G12"),
            data_for("pge", "G12"),
            at("2026-03-31T16:15:00+02:00"),
            hours=25,
        )
        self.assertEqual("dzienna", forecast.slots[0].zone)
        self.assertEqual("nocna", forecast.slots[24].zone)

    def test_uses_custom_energy_prices(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G12"),
            data_for("tauron", "G12"),
            at("2026-08-04T12:15:00+02:00"),
            custom_energy={"dzienna": 0.8, "nocna": 0.4},
            hours=2,
        )
        self.assertEqual("custom", forecast.energy_source)
        self.assertEqual([0.8, 0.4], [slot.energy_gross for slot in forecast.slots])

    def test_uses_active_distribution_and_system_rates(self) -> None:
        data = data_for("tauron", "G11")
        data.distribution_net = {"calodobowa": 0.5}
        data.system_net = {"quality": 0.05, "oze": 0.03, "cogeneration": 0.02}
        forecast = build_forecast(
            get_tariff("tauron", "G11"),
            data,
            at("2026-08-04T12:15:00+02:00"),
            hours=1,
        )
        slot = forecast.slots[0]
        self.assertEqual(0.615, slot.network_gross)
        self.assertEqual(0.123, slot.system_gross)
        self.assertEqual(0.738, slot.distribution_gross)

    def test_dynamic_forecast_stops_at_first_unpublished_kompas_hour(self) -> None:
        tariff = get_tariff("tauron", "G14dynamic")
        data = data_for("tauron", "G14dynamic")
        start = at("2026-08-04T12:15:00+02:00")
        data.dynamic_zones = {
            dynamic_hour_key(start): "S2_normalne",
            dynamic_hour_key(at("2026-08-04T13:00:00+02:00")): (
                "S3_zalecane_oszczedzanie"
            ),
        }
        forecast = build_forecast(tariff, data, start, hours=3)
        self.assertEqual(
            ["S2_normalne", "S3_zalecane_oszczedzanie"],
            [slot.zone for slot in forecast.slots],
        )
        self.assertFalse(forecast.complete)

    def test_every_slot_has_consistent_sums(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G13"),
            data_for("tauron", "G13"),
            at("2026-08-04T12:15:00+02:00"),
        )
        for slot in forecast.slots:
            self.assertEqual(
                slot.price_gross,
                round(slot.energy_gross + slot.distribution_gross, 4),
            )
            self.assertLessEqual(
                abs(
                    slot.distribution_gross
                    - slot.network_gross
                    - slot.system_gross
                ),
                0.0001,
            )

    def test_rejects_naive_datetime(self) -> None:
        with self.assertRaisesRegex(ValueError, "strefę czasową"):
            build_forecast(
                get_tariff("tauron", "G11"),
                data_for("tauron", "G11"),
                datetime(2026, 8, 4, 12, 15),
            )

    def test_returns_valid_prefix_at_end_of_tariff(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G11"),
            data_for("tauron", "G11"),
            at("2026-12-31T12:15:00+01:00"),
        )
        self.assertEqual(12, len(forecast.slots))
        self.assertFalse(forecast.complete)
        self.assertEqual(
            at("2027-01-01T00:00:00+01:00"), forecast.slots[-1].end
        )

    def test_returns_empty_expired_forecast(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G11"),
            data_for("tauron", "G11"),
            at("2027-01-01T00:15:00+01:00"),
        )
        self.assertEqual((), forecast.slots)
        self.assertFalse(forecast.complete)
        self.assertEqual("expired", forecast.source_status)

    def test_handles_spring_dst_on_the_utc_axis(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G11"),
            data_for("tauron", "G11"),
            datetime(2026, 3, 29, 1, 30, tzinfo=WARSAW),
            hours=2,
        )
        self.assertEqual(
            ["2026-03-29T01:00:00+01:00", "2026-03-29T03:00:00+02:00"],
            [slot.start.isoformat() for slot in forecast.slots],
        )

    def test_handles_both_fall_dst_hours(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G11"),
            data_for("tauron", "G11"),
            datetime(2026, 10, 25, 1, 30, tzinfo=WARSAW),
            hours=3,
        )
        starts = [slot.start.isoformat() for slot in forecast.slots]
        self.assertEqual(
            [
                "2026-10-25T01:00:00+02:00",
                "2026-10-25T02:00:00+02:00",
                "2026-10-25T02:00:00+01:00",
            ],
            starts,
        )
        starts_utc = [slot.start.astimezone(timezone.utc) for slot in forecast.slots]
        self.assertEqual(3, len(set(starts_utc)))

    def test_serializes_common_and_home_assistant_formats(self) -> None:
        forecast = build_forecast(
            get_tariff("tauron", "G11"),
            data_for("tauron", "G11"),
            at("2026-08-04T12:15:00+02:00"),
            hours=2,
        )
        common = forecast_to_dict(forecast)
        attributes = forecast_attributes(forecast)
        self.assertEqual(1, common["schema_version"])
        self.assertEqual("polish_energy_price", common["provider"])
        self.assertEqual("tauron:g11", common["tariff_id"])
        self.assertEqual("tauron", common["operator_id"])
        self.assertEqual("G11", common["tariff_code"])
        self.assertEqual("TAURON Dystrybucja S.A. G11", common["tariff_name"])
        self.assertEqual("PLN/kWh", common["unit"])
        self.assertEqual(2, len(common["slots"]))
        self.assertEqual(2, len(attributes["forecast"]))
        self.assertEqual("polish_energy_price", attributes["provider"])
        self.assertEqual("tauron:g11", attributes["tariff_id"])
        self.assertEqual("tauron", attributes["operator_id"])
        self.assertEqual("G11", attributes["tariff_code"])
        self.assertEqual(
            "TAURON Dystrybucja S.A. G11", attributes["tariff_name"]
        )
        self.assertEqual("PLN/kWh", attributes["unit"])
        self.assertEqual(60, attributes["forecast_resolution_min"])
        self.assertEqual("current", attributes["forecast_source_status"])
        self.assertEqual("2026-01-01", attributes["forecast_valid_from"])
        self.assertEqual("2026-12-31", attributes["forecast_valid_until"])
        self.assertNotIn("network_gross", attributes["forecast"][0])

    def test_resizes_a_precomputed_forecast(self) -> None:
        original = build_forecast(
            get_tariff("tauron", "G11"),
            data_for("tauron", "G11"),
            at("2026-08-04T12:15:00+02:00"),
            hours=168,
        )
        resized = resize_forecast(original, 24)
        self.assertEqual(24, resized.requested_hours)
        self.assertEqual(24, len(resized.slots))
        self.assertTrue(resized.complete)


if __name__ == "__main__":
    unittest.main()
