"""Tests for the platform-independent source engine."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import json
import unittest

from custom_components.polish_energy_price.kompas import local_day_for_key
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

    def test_newer_revision_replaces_zones_and_bumps_last_updated(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        first_fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 15:00:00"
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        first = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), first_fetch, now)
        )
        self.assertEqual(now.isoformat(), first.dynamic_last_updated)

        later = datetime(2026, 8, 4, 12, 30, tzinfo=WARSAW)
        second_fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 2, publication="2026-08-04 09:00:00"
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        second = asyncio.run(
            engine._refresh_dynamic(first, later.isoformat(), second_fetch, later)
        )
        self.assertEqual(later.isoformat(), second.dynamic_last_updated)
        self.assertEqual(
            {"S3_zalecane_oszczedzanie"}, self._zones_for_day(second, 2026, 8, 4)
        )

    def test_same_publication_timestamp_keeps_last_updated(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 15:00:00"
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        first = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), fetch, now)
        )

        later = datetime(2026, 8, 4, 12, 30, tzinfo=WARSAW)
        second = asyncio.run(
            engine._refresh_dynamic(first, later.isoformat(), fetch, later)
        )
        self.assertEqual(first.dynamic_last_updated, second.dynamic_last_updated)
        self.assertEqual(later.isoformat(), second.dynamic_last_checked)
        self.assertEqual(first.dynamic_zones, second.dynamic_zones)

    def test_older_revision_is_ignored(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        first_fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 16:00:00"
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        first = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), first_fetch, now)
        )

        later = datetime(2026, 8, 4, 12, 30, tzinfo=WARSAW)
        stale_fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    # Older publication_ts_utc than the one already stored.
                    "2026-08-04 10:00",
                    2,
                    publication="2026-08-03 15:00:00",
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        second = asyncio.run(
            engine._refresh_dynamic(first, later.isoformat(), stale_fetch, later)
        )
        self.assertEqual(first.dynamic_zones, second.dynamic_zones)
        self.assertEqual(first.dynamic_last_updated, second.dynamic_last_updated)
        self.assertEqual(
            first.dynamic_publications, second.dynamic_publications
        )

    def test_republication_with_identical_zones_updates_marker_only(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 15:00:00"
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        first = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), fetch, now)
        )

        later = datetime(2026, 8, 4, 12, 30, tzinfo=WARSAW)
        republished = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    # Newer marker, but the same zone value as before.
                    "2026-08-04 10:00",
                    1,
                    publication="2026-08-04 08:00:00",
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        second = asyncio.run(
            engine._refresh_dynamic(first, later.isoformat(), republished, later)
        )
        self.assertEqual(first.dynamic_zones, second.dynamic_zones)
        self.assertEqual(first.dynamic_last_updated, second.dynamic_last_updated)
        self.assertEqual(
            "2026-08-04T08:00:00+00:00",
            (second.dynamic_publications or {})["2026-08-04"],
        )

    def test_next_day_not_yet_published_is_not_an_error(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 15:00:00"
                ),
                "2026-08-05": json.dumps({"value": []}).encode(),
            }
        )
        result = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), fetch, now)
        )
        self.assertIsNone(result.dynamic_error)
        self.assertEqual({"S2_normalne"}, self._zones_for_day(result, 2026, 8, 4))
        self.assertEqual(set(), self._zones_for_day(result, 2026, 8, 5))

    def test_fetch_error_for_next_day_preserves_current_day(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 15:00:00"
                ),
                "2026-08-05": TimeoutError("brak odpowiedzi"),
            }
        )
        result = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), fetch, now)
        )
        self.assertIn("2026-08-05", result.dynamic_error or "")
        self.assertEqual({"S2_normalne"}, self._zones_for_day(result, 2026, 8, 4))

    def test_dynamic_publications_round_trip_and_missing_field_is_tolerated(
        self,
    ) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 15:00:00"
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        refreshed = asyncio.run(
            engine._refresh_dynamic(current, now.isoformat(), fetch, now)
        )
        self.assertTrue(refreshed.dynamic_publications)

        payload = engine.cache_payload(refreshed)
        restored = engine.data_from_cache(payload)
        self.assertEqual(
            refreshed.dynamic_publications, restored.dynamic_publications
        )

        payload.pop("dynamic_publications")
        restored_without_field = engine.data_from_cache(payload)
        self.assertEqual({}, restored_without_field.dynamic_publications)

    def test_retention_window_prunes_zones_and_publications(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        current = engine.initial_data()
        old_now = datetime(2026, 8, 1, 12, tzinfo=WARSAW)
        seed_fetch = self._fetch_map(
            {
                "2026-08-01": self._kompas_payload(
                    "2026-08-01 10:00", 1, publication="2026-07-31 15:00:00"
                ),
                "2026-08-02": self._kompas_payload(
                    "2026-08-02 10:00", 1, publication="2026-07-31 15:30:00"
                ),
            }
        )
        seeded = asyncio.run(
            engine._refresh_dynamic(current, old_now.isoformat(), seed_fetch, old_now)
        )
        self.assertIn("2026-08-01", seeded.dynamic_publications or {})
        self.assertIn("2026-08-02", seeded.dynamic_publications or {})

        later_now = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        later_fetch = self._fetch_map(
            {
                "2026-08-04": self._kompas_payload(
                    "2026-08-04 10:00", 1, publication="2026-08-03 15:00:00"
                ),
                "2026-08-05": self._kompas_payload(
                    "2026-08-05 10:00", 1, publication="2026-08-03 15:30:00"
                ),
            }
        )
        later = asyncio.run(
            engine._refresh_dynamic(
                seeded, later_now.isoformat(), later_fetch, later_now
            )
        )
        self.assertNotIn("2026-08-01", later.dynamic_publications or {})
        self.assertNotIn("2026-08-02", later.dynamic_publications or {})
        self.assertEqual(set(), self._zones_for_day(later, 2026, 8, 1))
        self.assertEqual(set(), self._zones_for_day(later, 2026, 8, 2))

    @staticmethod
    def _zones_for_day(data, year: int, month: int, day: int) -> set[str]:
        target = date(year, month, day)
        return {
            zone
            for key, zone in (data.dynamic_zones or {}).items()
            if local_day_for_key(key) == target
        }

    @staticmethod
    def _fetch_map(responses: dict[str, object]):
        async def fetch(url, *_args, **_kwargs):
            for day, response in responses.items():
                if day in url:
                    if isinstance(response, BaseException):
                        raise response
                    return response
            raise AssertionError(f"Nieoczekiwany URL Kompasu: {url}")

        return fetch

    @staticmethod
    def _kompas_payload(
        utc_hour: str, level: int, *, publication: str = "2026-08-03 15:00:00"
    ) -> bytes:
        return json.dumps(
            {
                "value": [
                    {
                        "dtime_utc": utc_hour,
                        "is_active": True,
                        "usage_fcst": level,
                        "publication_ts_utc": publication,
                    }
                ]
            }
        ).encode()


if __name__ == "__main__":
    unittest.main()
