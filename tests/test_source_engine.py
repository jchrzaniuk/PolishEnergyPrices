"""Tests for the platform-independent source engine."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
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

    def test_export_rce_cache_round_trip(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rce"
        )
        original = engine.initial_data()
        self.assertEqual({}, original.export_prices)
        self.assertEqual({}, original.export_publications)

        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-09-10": self._rce_payload(date(2026, 9, 10), 700.0),
                "2026-09-11": self._rce_payload(date(2026, 9, 11), 725.66),
                "2026-09-12": self._rce_payload(date(2026, 9, 12), 800.0),
            }
        )
        refreshed = asyncio.run(
            engine._refresh_rce(original, now.isoformat(), fetch, now)
        )
        self.assertTrue(refreshed.export_prices)
        payload = engine.cache_payload(refreshed)
        restored = engine.data_from_cache(payload)
        self.assertEqual(refreshed.export_prices, restored.export_prices)
        self.assertEqual(
            refreshed.export_publications, restored.export_publications
        )

    def test_export_rcem_cache_round_trip(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rcem"
        )
        original = engine.initial_data()
        self.assertEqual({}, original.rcem_prices)

        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)

        async def fetch(*_args, **_kwargs):
            return RCEM_HTML_FIXTURE.encode()

        refreshed = asyncio.run(
            engine._refresh_rcem(original, now.isoformat(), fetch, now)
        )
        self.assertTrue(refreshed.rcem_prices)
        payload = engine.cache_payload(refreshed)
        restored = engine.data_from_cache(payload)
        self.assertEqual(refreshed.rcem_prices, restored.rcem_prices)

    def test_export_rcem_september_fetches_both_years_from_one_page(self) -> None:
        # Outside January too, both the current and previous year's tables
        # must be picked up from the single fetched page, so a correction to
        # last year's December (or any other month of that year) still
        # keeps refreshing all year round.
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rcem"
        )
        current = engine.initial_data()
        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        calls: list[str] = []

        async def fetch(url, *_args, **_kwargs):
            calls.append(url)
            return RCEM_HTML_TWO_YEARS.encode()

        refreshed = asyncio.run(
            engine._refresh_rcem(current, now.isoformat(), fetch, now)
        )
        self.assertEqual(1, len(calls))
        self.assertEqual(
            {"2026-01", "2025-01"}, set(refreshed.rcem_prices or {})
        )
        self.assertIsNone(refreshed.rcem_error)

    def test_export_rcem_january_missing_current_year_table_is_not_an_error(
        self,
    ) -> None:
        # Early January: only last year's table exists yet (this year's
        # first price is published 11 February), so the December price is
        # still picked up and this must not be reported as an error.
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rcem"
        )
        current = engine.initial_data()
        now = datetime(2027, 1, 15, 12, tzinfo=WARSAW)

        async def fetch(*_args, **_kwargs):
            return RCEM_HTML_DECEMBER_ONLY.encode()

        refreshed = asyncio.run(
            engine._refresh_rcem(current, now.isoformat(), fetch, now)
        )
        self.assertEqual({"2026-12": 0.3}, refreshed.rcem_prices)
        self.assertIsNone(refreshed.rcem_error)

    def test_export_rcem_january_missing_both_years_sets_error(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rcem"
        )
        current = engine.initial_data()
        now = datetime(2027, 1, 15, 12, tzinfo=WARSAW)

        async def fetch(*_args, **_kwargs):
            return b"<html><body>nowy uklad strony bez tabel RCEm</body></html>"

        refreshed = asyncio.run(
            engine._refresh_rcem(current, now.isoformat(), fetch, now)
        )
        self.assertEqual({}, refreshed.rcem_prices)
        self.assertIn("2027", refreshed.rcem_error or "")
        self.assertIn("2026", refreshed.rcem_error or "")

    def test_export_rce_newer_publication_updates_price_and_last_updated(
        self,
    ) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rce"
        )
        current = engine.initial_data()
        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        first_fetch = self._fetch_map(
            {
                "2026-09-10": self._rce_payload(
                    date(2026, 9, 10), 700.0, publication="2026-09-09 12:00:00.000"
                ),
                "2026-09-11": self._rce_payload(
                    date(2026, 9, 11), 725.66, publication="2026-09-10 12:17:22.709"
                ),
                "2026-09-12": self._rce_payload(
                    date(2026, 9, 12), 800.0, publication="2026-09-10 14:00:00.000"
                ),
            }
        )
        first = asyncio.run(
            engine._refresh_rce(current, now.isoformat(), first_fetch, now)
        )
        self.assertEqual(now.isoformat(), first.export_last_updated)

        # Past the 1h throttle window, so a real fetch is attempted again.
        later = datetime(2026, 9, 11, 13, 30, tzinfo=WARSAW)
        second_fetch = self._fetch_map(
            {
                "2026-09-10": self._rce_payload(
                    date(2026, 9, 10), 700.0, publication="2026-09-09 12:00:00.000"
                ),
                "2026-09-11": self._rce_payload(
                    date(2026, 9, 11), 900.0, publication="2026-09-11 13:00:00.000"
                ),
                "2026-09-12": self._rce_payload(
                    date(2026, 9, 12), 800.0, publication="2026-09-10 14:00:00.000"
                ),
            }
        )
        second = asyncio.run(
            engine._refresh_rce(first, later.isoformat(), second_fetch, later)
        )
        self.assertEqual(later.isoformat(), second.export_last_updated)
        self.assertEqual(
            0.9, second.export_prices["2026-09-11T12:00:00+00:00"]
        )

    def test_export_rce_same_publication_keeps_last_updated(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rce"
        )
        current = engine.initial_data()
        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-09-10": self._rce_payload(
                    date(2026, 9, 10), 700.0, publication="2026-09-09 12:00:00.000"
                ),
                "2026-09-11": self._rce_payload(
                    date(2026, 9, 11), 725.66, publication="2026-09-10 12:17:22.709"
                ),
                "2026-09-12": self._rce_payload(
                    date(2026, 9, 12), 800.0, publication="2026-09-10 14:00:00.000"
                ),
            }
        )
        first = asyncio.run(
            engine._refresh_rce(current, now.isoformat(), fetch, now)
        )

        later = datetime(2026, 9, 11, 13, 30, tzinfo=WARSAW)
        second = asyncio.run(
            engine._refresh_rce(first, later.isoformat(), fetch, later)
        )
        self.assertEqual(first.export_last_updated, second.export_last_updated)
        self.assertEqual(later.isoformat(), second.export_last_checked)
        self.assertEqual(first.export_prices, second.export_prices)

    def test_export_rce_fetch_error_preserves_previous_prices(self) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rce"
        )
        current = engine.initial_data()
        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        fetch = self._fetch_map(
            {
                "2026-09-10": self._rce_payload(date(2026, 9, 10), 700.0),
                "2026-09-11": self._rce_payload(date(2026, 9, 11), 725.66),
                "2026-09-12": self._rce_payload(date(2026, 9, 12), 800.0),
            }
        )
        first = asyncio.run(
            engine._refresh_rce(current, now.isoformat(), fetch, now)
        )

        later = datetime(2026, 9, 11, 14, 0, tzinfo=WARSAW)
        failing_fetch = self._fetch_map(
            {
                "2026-09-10": self._rce_payload(date(2026, 9, 10), 700.0),
                "2026-09-11": TimeoutError("brak odpowiedzi"),
                "2026-09-12": self._rce_payload(date(2026, 9, 12), 800.0),
            }
        )
        second = asyncio.run(
            engine._refresh_rce(first, later.isoformat(), failing_fetch, later)
        )
        self.assertIn("brak odpowiedzi", second.export_error or "")
        self.assertEqual(first.export_prices, second.export_prices)

    def test_export_rce_throttle_skips_without_next_day_prices(self) -> None:
        # D+1 is normally not published before ~14:00; the throttle must not
        # require it, or a 15-minute refresh cadence would call PSE far more
        # often than the hourly cap intends while waiting for it.
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rce"
        )
        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        current = replace(
            engine.initial_data(),
            export_prices={"2026-09-11T12:00:00+00:00": 0.7},
            export_last_checked=(now - timedelta(minutes=30)).isoformat(),
        )

        async def fetch_must_not_be_called(*_args, **_kwargs):
            raise AssertionError(
                "fetch nie powinien zostać wywołany w oknie throttle'a 1h"
            )

        result = asyncio.run(
            engine._refresh_rce(
                current, now.isoformat(), fetch_must_not_be_called, now
            )
        )
        self.assertIs(current, result)

    def test_export_rce_missing_current_day_forces_fetch_despite_fresh_check(
        self,
    ) -> None:
        engine = EnergyPriceSourceEngine(
            "tauron", "G11", "regulated", export_settlement="rce"
        )
        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        # export_last_checked is fresh (30 minutes ago), but there are no
        # prices at all for the current day, so the throttle must not skip.
        current = replace(
            engine.initial_data(),
            export_prices={},
            export_last_checked=(now - timedelta(minutes=30)).isoformat(),
        )
        fetch = self._fetch_map(
            {
                "2026-09-10": self._rce_payload(date(2026, 9, 10), 700.0),
                "2026-09-11": self._rce_payload(date(2026, 9, 11), 725.66),
                "2026-09-12": self._rce_payload(date(2026, 9, 12), 800.0),
            }
        )
        result = asyncio.run(
            engine._refresh_rce(current, now.isoformat(), fetch, now)
        )
        self.assertEqual(now.isoformat(), result.export_last_checked)
        self.assertEqual(
            0.72566, result.export_prices["2026-09-11T12:00:00+00:00"]
        )

    def test_no_export_settlement_never_calls_pse_for_rce(self) -> None:
        engine = EnergyPriceSourceEngine("tauron", "G11", "regulated")
        current = engine.initial_data()
        now = datetime(2026, 9, 11, 12, tzinfo=WARSAW)
        calls: list[str] = []

        async def counting_fetch(url, *_args, **_kwargs):
            calls.append(url)
            raise AssertionError("fetch nie powinien być wywołany")

        result = asyncio.run(
            engine._refresh_export(current, now.isoformat(), counting_fetch, now)
        )
        self.assertIs(current, result)
        self.assertEqual([], calls)

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

    @staticmethod
    def _rce_payload(
        day: date, rce_pln: float, *, publication: str = "2026-09-09 12:00:00.000"
    ) -> bytes:
        # A single midday 15-minute row is enough to mark the day as known;
        # noon UTC always falls on the same Warsaw calendar day as ``day``.
        return json.dumps(
            {
                "value": [
                    {
                        "dtime_utc": f"{day.isoformat()} 12:15:00",
                        "period_utc": "12:00 - 12:15",
                        "rce_pln": rce_pln,
                        "publication_ts_utc": publication,
                    }
                ]
            }
        ).encode()


# Tabela przycięta z prawdziwej strony RCEm PSE, wystarczająca do sprawdzenia
# round-tripu cache dla trybu "rcem" (patrz też tests/test_export_price.py).
RCEM_HTML_FIXTURE = """
<table><tbody>
<tr><th align="center" colspan="4"><strong>2026</strong></th></tr>
<tr><td bgcolor="#eeeeee">&nbsp;</td><td align="center">cena [zł/MWh]</td>
<td align="center">data publikacji</td><td align="center">różnica</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><strong>styczeń</strong></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">551,96</td>
<td align="center">11.02.2026</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">-</td>
<td align="center">-</td><td align="center">-</td></tr>
</tbody></table>
"""

# Both the current year's and the previous year's tables on one page, as
# they normally sit together outside of early January.
RCEM_HTML_TWO_YEARS = """
<table><tbody>
<tr><th align="center" colspan="4"><strong>2026</strong></th></tr>
<tr><td bgcolor="#eeeeee">&nbsp;</td><td align="center">cena [zł/MWh]</td>
<td align="center">data publikacji</td><td align="center">różnica</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><strong>styczeń</strong></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">551,96</td>
<td align="center">11.02.2026</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">-</td>
<td align="center">-</td><td align="center">-</td></tr>
</tbody></table>
<table><tbody>
<tr><th align="center" colspan="4"><strong>2025</strong></th></tr>
<tr><td bgcolor="#eeeeee">&nbsp;</td><td align="center">cena [zł/MWh]</td>
<td align="center">data publikacji</td><td align="center">różnica</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><strong>styczeń</strong></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">100,00</td>
<td align="center">11.02.2025</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">-</td>
<td align="center">-</td><td align="center">-</td></tr>
</tbody></table>
"""

# Only last year's table exists, as on the real page in early January
# before the current year's first table (11 February) is published.
RCEM_HTML_DECEMBER_ONLY = """
<table><tbody>
<tr><th align="center" colspan="4"><strong>2026</strong></th></tr>
<tr><td bgcolor="#eeeeee">&nbsp;</td><td align="center">cena [zł/MWh]</td>
<td align="center">data publikacji</td><td align="center">różnica</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><b>grudzień</b></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">300,00</td>
<td align="center">11.01.2027</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">-</td>
<td align="center">-</td><td align="center">-</td></tr>
</tbody></table>
"""


if __name__ == "__main__":
    unittest.main()
