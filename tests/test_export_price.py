"""Tests for RCE/RCEm export-price parsing (net-billing, etap 1)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from custom_components.polish_energy_price.export_price import (
    build_rce_url,
    export_price_at,
    negative_periods,
    parse_rce,
    parse_rcem,
    upcoming_export_periods,
)
from custom_components.polish_energy_price.tariff import WARSAW

# Wiersz przycięty z prawdziwej odpowiedzi API PSE (rce-2026-09-11.json).
RCE_ROW_15MIN = {
    "dtime_utc": "2026-09-10 22:15:00",
    "period_utc": "22:00 - 22:15",
    "rce_pln": 725.66,
    "publication_ts_utc": "2026-09-10 12:17:22.709",
}

# Tabele przycięte z prawdziwej strony PSE (rcem.html).
RCEM_HTML = """
<table><tbody>
<tr><th align="center" colspan="4"><strong>2026</strong></th></tr>
<tr><td bgcolor="#eeeeee">&nbsp;</td><td align="center">cena [zł/MWh]</td>
<td align="center">data publikacji</td><td align="center">różnica</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><strong>styczeń</strong></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">551,96</td>
<td align="center">11.02.2026</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap"><span>skorygowana RCEm*</span></td><td align="right">-</td>
<td align="center">-</td><td align="center">-</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><strong>luty</strong></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">339,01</td>
<td align="center">11.03.2026</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">331,39</td>
<td align="center">11.06.2026</td><td align="center">- 2,25</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><strong>marzec***&nbsp;</strong></td></tr>
<tr><td nowrap="nowrap"><span>RCEm</span></td><td align="right">191,95</td>
<td align="center">11.04.2026</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">-</td>
<td align="center">-</td><td align="center">-</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><b>kwiecień</b></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">-</td>
<td align="center">-</td><td align="center">-</td></tr>
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

# Maj 2024 miał dwa wiersze skorygowane — obowiązuje ostatni z wartością.
RCEM_HTML_DOUBLE_CORRECTION = """
<table><tbody>
<tr><th align="center" colspan="4"><strong>2024</strong></th></tr>
<tr><td bgcolor="#eeeeee">&nbsp;</td><td align="center">cena [zł/MWh]</td>
<td align="center">data publikacji</td><td align="center">różnica</td></tr>
<tr><td bgcolor="#eeeeee" colspan="4"><b>maj</b></td></tr>
<tr><td nowrap="nowrap">RCEm</td><td align="right">255,59</td>
<td align="center">11.06.2024</td><td align="center">-</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">255,32</td>
<td align="center">11.07.2024</td><td align="center">-0,11</td></tr>
<tr><td nowrap="nowrap">skorygowana RCEm*</td><td align="right">254,19</td>
<td align="center">11.09.2024</td><td align="center">-0,44</td></tr>
</tbody></table>
"""

RCEM_HTML_BROKEN_LAYOUT = """
<table><tbody>
<tr><th align="center" colspan="4"><strong>2030</strong></th></tr>
<tr><td colspan="4">nowy układ strony bez tabeli cen</td></tr>
</tbody></table>
"""


class ParseRceTests(unittest.TestCase):
    def test_parses_15_minute_row_key_and_publication(self) -> None:
        snapshot = parse_rce(json.dumps({"value": [RCE_ROW_15MIN]}))
        self.assertEqual(
            {"2026-09-10T22:00:00+00:00": 0.72566}, snapshot.prices
        )
        self.assertEqual(
            "2026-09-10T12:17:22.709000+00:00", snapshot.publication_utc
        )

    def test_accepts_60_minute_row_and_rejects_other_lengths(self) -> None:
        row_60 = {
            "dtime_utc": "2024-01-01 01:00:00",
            "period_utc": "00:00 - 01:00",
            "rce_pln": 300.0,
        }
        snapshot = parse_rce(json.dumps({"value": [row_60]}))
        self.assertEqual({"2024-01-01T00:00:00+00:00": 0.3}, snapshot.prices)

        row_30 = {**row_60, "period_utc": "00:30 - 01:00"}
        with self.assertRaisesRegex(ValueError, "długość okresu"):
            parse_rce(json.dumps({"value": [row_30]}))

    def test_rejects_malformed_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "listy value"):
            parse_rce(json.dumps({"nope": []}))

        string_price = {**RCE_ROW_15MIN, "rce_pln": "725.66"}
        with self.assertRaisesRegex(ValueError, "rce_pln"):
            parse_rce(json.dumps({"value": [string_price]}))

        bool_price = {**RCE_ROW_15MIN, "rce_pln": True}
        with self.assertRaisesRegex(ValueError, "rce_pln"):
            parse_rce(json.dumps({"value": [bool_price]}))

        out_of_range = {**RCE_ROW_15MIN, "rce_pln": 6000.0}
        with self.assertRaisesRegex(ValueError, "zakres"):
            parse_rce(json.dumps({"value": [out_of_range]}))

        conflicting = {**RCE_ROW_15MIN, "rce_pln": 1.0}
        with self.assertRaisesRegex(ValueError, "dwie różne ceny"):
            parse_rce(json.dumps({"value": [RCE_ROW_15MIN, conflicting]}))

    def test_accepts_negative_price(self) -> None:
        negative = {**RCE_ROW_15MIN, "rce_pln": -50.0}
        snapshot = parse_rce(json.dumps({"value": [negative]}))
        self.assertEqual(-0.05, snapshot.prices["2026-09-10T22:00:00+00:00"])

    def test_build_rce_url_has_no_is_active_filter(self) -> None:
        from datetime import date

        url = build_rce_url(date(2026, 9, 11))
        self.assertIn("business_date%20eq%20%272026-09-11%27", url)
        self.assertNotIn("is_active", url)


class ParseRcemTests(unittest.TestCase):
    def test_corrected_price_wins_over_base(self) -> None:
        result = parse_rcem(RCEM_HTML, 2026)
        self.assertEqual(0.33139, result["2026-02"])

    def test_month_without_value_is_omitted(self) -> None:
        result = parse_rcem(RCEM_HTML, 2026)
        self.assertNotIn("2026-04", result)

    def test_asterisks_and_nbsp_in_month_name_are_tolerated(self) -> None:
        result = parse_rcem(RCEM_HTML, 2026)
        self.assertEqual(0.19195, result["2026-03"])

    def test_table_selection_by_year(self) -> None:
        result_2025 = parse_rcem(RCEM_HTML, 2025)
        self.assertEqual({"2025-01": 0.1}, result_2025)
        result_2026 = parse_rcem(RCEM_HTML, 2026)
        self.assertNotIn("2025-01", result_2026)

    def test_changed_layout_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_rcem(RCEM_HTML_BROKEN_LAYOUT, 2030)

    def test_last_corrected_row_wins_when_month_has_two(self) -> None:
        result = parse_rcem(RCEM_HTML_DOUBLE_CORRECTION, 2024)
        self.assertEqual({"2024-05": 0.25419}, result)


class ExportPriceAtTests(unittest.TestCase):
    def test_rce_mode_hits_the_right_15_minute_period(self) -> None:
        ts = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        rce_prices = {"2026-09-10T22:00:00+00:00": 0.72566}
        result = export_price_at(ts, settlement="rce", rce_prices=rce_prices)
        self.assertEqual("2026-09-10T22:00:00+00:00", result.period)
        self.assertEqual(0.72566, result.raw)
        self.assertEqual(0.72566, result.value)

    def test_correction_multiplies_value_but_not_raw(self) -> None:
        ts = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        rce_prices = {"2026-09-10T22:00:00+00:00": 0.72566}
        result = export_price_at(
            ts, settlement="rce", rce_prices=rce_prices, correction=1.23
        )
        self.assertEqual(0.72566, result.raw)
        self.assertEqual(round(0.72566 * 1.23, 6), result.value)

    def test_negative_rce_clamps_value_to_zero_but_keeps_raw(self) -> None:
        ts = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        rce_prices = {"2026-09-10T22:00:00+00:00": -0.05}
        result = export_price_at(ts, settlement="rce", rce_prices=rce_prices)
        self.assertEqual(-0.05, result.raw)
        self.assertEqual(0.0, result.value)

        corrected = export_price_at(
            ts, settlement="rce", rce_prices=rce_prices, correction=1.23
        )
        self.assertEqual(-0.05, corrected.raw)
        self.assertEqual(0.0, corrected.value)

    def test_missing_data_returns_none(self) -> None:
        ts = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        self.assertIsNone(
            export_price_at(ts, settlement="rce", rce_prices={})
        )

    def test_rcem_mode_uses_last_known_month_during_the_month(self) -> None:
        rcem_prices = {"2026-01": 0.5, "2026-02": 0.33139}
        ts = datetime(2026, 3, 15, 12, tzinfo=WARSAW)
        result = export_price_at(ts, settlement="rcem", rcem_prices=rcem_prices)
        self.assertEqual("2026-02", result.period)
        self.assertEqual(0.33139, result.raw)

    def test_naive_timestamp_and_unknown_settlement_raise(self) -> None:
        rce_prices = {"2026-09-10T22:00:00+00:00": 0.72566}
        with self.assertRaises(ValueError):
            export_price_at(
                datetime(2026, 9, 10, 22, 5), settlement="rce", rce_prices=rce_prices
            )
        ts = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            export_price_at(ts, settlement="unknown", rce_prices=rce_prices)


class UpcomingExportPeriodsTests(unittest.TestCase):
    def test_ascending_order_and_past_periods_are_skipped(self) -> None:
        now = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        prices = {
            "2026-09-10T21:45:00+00:00": 0.10,  # przeszłość — pominięte
            "2026-09-10T22:15:00+00:00": 0.30,
            "2026-09-10T22:00:00+00:00": 0.20,  # bieżący okres
        }
        result = upcoming_export_periods(prices, now)
        self.assertEqual([0.20, 0.30], [item["rce_netto"] for item in result])

    def test_respects_limit(self) -> None:
        now = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
        prices = {
            f"2026-09-{day:02d}T00:00:00+00:00": 0.10 for day in range(10, 20)
        }
        result = upcoming_export_periods(prices, now, limit=3)
        self.assertEqual(3, len(result))

    def test_correction_multiplies_cena_netto_but_not_rce_netto(self) -> None:
        now = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        prices = {"2026-09-10T22:00:00+00:00": 0.72566}
        result = upcoming_export_periods(prices, now, correction=1.23)
        self.assertEqual(0.72566, result[0]["rce_netto"])
        self.assertEqual(round(0.72566 * 1.23, 6), result[0]["cena_netto"])

    def test_negative_rce_clamps_cena_netto_to_zero_but_keeps_rce_netto(self) -> None:
        now = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        prices = {"2026-09-10T22:00:00+00:00": -0.05}
        result = upcoming_export_periods(prices, now, correction=1.23)
        self.assertEqual(-0.05, result[0]["rce_netto"])
        self.assertEqual(0.0, result[0]["cena_netto"])

    def test_empty_mapping_returns_empty_list(self) -> None:
        now = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        self.assertEqual([], upcoming_export_periods({}, now))

    def test_start_is_formatted_in_warsaw_time(self) -> None:
        now = datetime(2026, 9, 10, 22, 5, tzinfo=timezone.utc)
        prices = {"2026-09-10T22:00:00+00:00": 0.20}
        result = upcoming_export_periods(prices, now)
        self.assertEqual("2026-09-11T00:00:00+02:00", result[0]["start"])


class NegativePeriodsTests(unittest.TestCase):
    def test_returns_only_negative_periods_in_order(self) -> None:
        periods = [
            {"start": "a", "cena_netto": 0.0, "rce_netto": -0.05},
            {"start": "b", "cena_netto": 0.2, "rce_netto": 0.20},
            {"start": "c", "cena_netto": 0.0, "rce_netto": -0.01},
        ]
        result = negative_periods(periods)
        self.assertEqual([periods[0], periods[2]], result)

    def test_all_positive_periods_return_empty_list(self) -> None:
        periods = [{"start": "a", "cena_netto": 0.1, "rce_netto": 0.1}]
        self.assertEqual([], negative_periods(periods))

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual([], negative_periods([]))


if __name__ == "__main__":
    unittest.main()
