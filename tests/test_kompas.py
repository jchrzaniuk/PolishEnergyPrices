"""Tests for the Energetyczny Kompas PSE parser."""

from __future__ import annotations

from datetime import date
import json
import unittest

from custom_components.polish_energy_price.kompas import (
    build_kompas_url,
    parse_kompas,
    validate_dynamic_zones,
)


class KompasTests(unittest.TestCase):
    def test_builds_encoded_active_revision_query(self) -> None:
        url = build_kompas_url(date(2026, 8, 4))
        self.assertIn("business_date%20eq%20%272026-08-04%27", url)
        self.assertIn("is_active%20eq%20true", url)
        self.assertIn("%24filter=", url)

    def test_maps_all_four_levels_using_utc_hour_keys(self) -> None:
        rows = [
            {
                "dtime_utc": f"2026-08-04 {10 + level:02d}:00",
                "is_active": True,
                "usage_fcst": level,
                "publication_ts_utc": "2026-08-03 15:00:00",
            }
            for level in range(4)
        ]
        snapshot = parse_kompas(json.dumps({"value": rows}))
        self.assertEqual(
            [
                "S1_zalecane_uzytkowanie",
                "S2_normalne",
                "S3_zalecane_oszczedzanie",
                "S4_wymagane_ograniczenie",
            ],
            list(snapshot.zones.values()),
        )
        self.assertEqual(
            "2026-08-03T15:00:00+00:00", snapshot.publication_utc
        )

    def test_ignores_inactive_rows_and_rejects_unknown_levels(self) -> None:
        inactive = {
            "value": [
                {
                    "dtime_utc": "2026-08-04 10:00",
                    "is_active": False,
                    "usage_fcst": 3,
                }
            ]
        }
        self.assertEqual({}, parse_kompas(json.dumps(inactive)).zones)
        invalid = {
            "value": [
                {
                    "dtime_utc": "2026-08-04 10:00",
                    "is_active": True,
                    "usage_fcst": 4,
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "nieznany poziom"):
            parse_kompas(json.dumps(invalid))

    def test_cache_validation_normalizes_keys(self) -> None:
        result = validate_dynamic_zones(
            {"2026-08-04T12:00:00+02:00": "S2_normalne"}
        )
        self.assertEqual(
            {"2026-08-04T10:00:00+00:00": "S2_normalne"}, result
        )


if __name__ == "__main__":
    unittest.main()
