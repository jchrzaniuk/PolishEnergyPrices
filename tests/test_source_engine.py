"""Tests for the platform-independent source engine."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime
import json
import unittest

from custom_components.polish_energy_price.forecast import build_forecast
from custom_components.polish_energy_price.kompas import local_day_for_key
from custom_components.polish_energy_price.official import (
    TAURON_G13S_PAGE,
    TAURON_G14DYNAMIC_PRICE_LIST,
)
from custom_components.polish_energy_price.source_engine import (
    EnergyPriceSourceEngine,
)
from custom_components.polish_energy_price.tariff import WARSAW, get_tariff


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

    def test_healthy_cache_payload_keeps_plain_source_name(self) -> None:
        # A payload with no recorded error means the last fetch succeeded;
        # reloading it after a restart must not invent a "_cache" warning.
        cases = [
            ("regulated", "tauron", "G11", "ure", "https://ure.example/x.xlsx"),
            ("tauron_g13s", "tauron", "G13s", "tauron_g13s", TAURON_G13S_PAGE),
            (
                "tauron_g14dynamic",
                "tauron",
                "G14dynamic",
                "tauron_g14dynamic",
                TAURON_G14DYNAMIC_PRICE_LIST,
            ),
        ]
        for price_source, operator, group, source_name, source_url in cases:
            with self.subTest(price_source=price_source):
                engine = EnergyPriceSourceEngine(operator, group, price_source)
                healthy = replace(
                    engine.initial_data(),
                    source=source_name,
                    source_url=source_url,
                    last_checked="2026-08-04T08:00:00+02:00",
                    last_updated="2026-08-04T08:00:00+02:00",
                    error=None,
                )
                payload = engine.cache_payload(healthy)
                restored = engine.data_from_cache(payload)
                self.assertEqual(source_name, restored.source)
                self.assertIsNone(restored.error)

    def test_cache_payload_with_stored_error_gets_cache_suffix(self) -> None:
        # A payload that recorded a failed fetch must keep signalling that
        # the value is a last-known-good fallback, including when it was
        # already saved with the "_cache" suffix from an earlier failure.
        cases = [
            ("regulated", "tauron", "G11", "ure", "ure_cache"),
            ("regulated", "tauron", "G11", "ure_cache", "ure_cache"),
            ("tauron_g13s", "tauron", "G13s", "tauron_g13s", "tauron_g13s_cache"),
            (
                "tauron_g13s",
                "tauron",
                "G13s",
                "tauron_g13s_cache",
                "tauron_g13s_cache",
            ),
            (
                "tauron_g14dynamic",
                "tauron",
                "G14dynamic",
                "tauron_g14dynamic",
                "tauron_g14dynamic_cache",
            ),
            (
                "tauron_g14dynamic",
                "tauron",
                "G14dynamic",
                "tauron_g14dynamic_cache",
                "tauron_g14dynamic_cache",
            ),
        ]
        for price_source, operator, group, source_name, expected in cases:
            with self.subTest(price_source=price_source, source_name=source_name):
                engine = EnergyPriceSourceEngine(operator, group, price_source)
                failed = replace(
                    engine.initial_data(),
                    source=source_name,
                    source_url="https://example.invalid/last-known-good",
                    last_checked="2026-08-04T08:00:00+02:00",
                    error="brak odpowiedzi",
                )
                payload = engine.cache_payload(failed)
                restored = engine.data_from_cache(payload)
                self.assertEqual(expected, restored.source)
                self.assertEqual("brak odpowiedzi", restored.error)

    def test_cache_payload_without_source_url_is_bundled(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        payload = engine.cache_payload(engine.initial_data())
        restored = engine.data_from_cache(payload)
        self.assertEqual("bundled", restored.source)

    def test_healthy_g14dynamic_survives_restart_without_false_warning(
        self,
    ) -> None:
        """Reproduces the reported bug end to end and proves the fix.

        Healthy state -> cache_payload -> data_from_cache (simulated
        restart) -> _refresh_energy inside the 12h throttle window (the
        fetch callback must not even be invoked) -> build_forecast must
        report "current", not "cache_or_warning".
        """

        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        tariff = get_tariff("tauron", "G14dynamic")
        healthy = replace(
            engine.initial_data(),
            source="tauron_g14dynamic",
            source_url=TAURON_G14DYNAMIC_PRICE_LIST,
            last_checked="2026-08-04T08:00:00+02:00",
            last_updated="2026-08-04T08:00:00+02:00",
            error=None,
        )

        payload = engine.cache_payload(healthy)
        restored = engine.data_from_cache(payload)
        self.assertEqual("tauron_g14dynamic", restored.source)

        async def fetch_must_not_be_called(*_args, **_kwargs):
            raise AssertionError(
                "fetch nie powinien zostać wywołany w oknie throttle'a 12h"
            )

        async def run_sync(function, *args):
            return function(*args)

        later = datetime(2026, 8, 4, 12, tzinfo=WARSAW)
        refreshed = asyncio.run(
            engine._refresh_energy(
                restored, later.isoformat(), fetch_must_not_be_called, run_sync
            )
        )
        self.assertEqual("tauron_g14dynamic", refreshed.source)
        self.assertIsNone(refreshed.error)

        forecast = build_forecast(tariff, refreshed, later, hours=1)
        self.assertEqual("current", forecast.source_status)

    def test_g14dynamic_fetch_failure_still_reports_cache_or_warning(
        self,
    ) -> None:
        """The opposite case: a real fetch failure must still warn."""

        engine = EnergyPriceSourceEngine(
            "tauron", "G14dynamic", "tauron_g14dynamic"
        )
        tariff = get_tariff("tauron", "G14dynamic")
        healthy = replace(
            engine.initial_data(),
            source="tauron_g14dynamic",
            source_url=TAURON_G14DYNAMIC_PRICE_LIST,
            last_checked="2026-08-04T08:00:00+02:00",
            last_updated="2026-08-04T08:00:00+02:00",
            error=None,
        )

        async def fetch_failure(*_args, **_kwargs):
            raise TimeoutError("brak odpowiedzi")

        async def run_sync(function, *args):
            return function(*args)

        # Outside the 12h throttle window, so a real fetch is attempted.
        later = datetime(2026, 8, 5, 0, tzinfo=WARSAW)
        refreshed = asyncio.run(
            engine._refresh_energy(
                healthy, later.isoformat(), fetch_failure, run_sync
            )
        )
        self.assertEqual("tauron_g14dynamic_cache", refreshed.source)
        self.assertIn("brak odpowiedzi", refreshed.error or "")

        forecast = build_forecast(tariff, refreshed, later, hours=1)
        self.assertEqual("cache_or_warning", forecast.source_status)

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
