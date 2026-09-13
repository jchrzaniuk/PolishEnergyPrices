"""Tests for dependency-free cost statistics conversion."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest

from custom_components.polish_energy_price.export_price import export_price_at
from custom_components.polish_energy_price.tariff import WARSAW

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "polish_energy_price" / "cost.py"
)
SPEC = importlib.util.spec_from_file_location("polish_energy_cost", MODULE_PATH)
assert SPEC and SPEC.loader
cost = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cost
SPEC.loader.exec_module(cost)


class CostStatisticsTests(unittest.TestCase):
    def test_generated_statistic_id_is_lowercase(self) -> None:
        self.assertEqual(
            "polish_energy_price:01kysan_cost_pozostale",
            cost.cost_statistic_id("01KYSAN", "pozostale"),
        )

    def test_multiplies_cumulative_kwh_by_gross_zone_price(self) -> None:
        rows = cost.cumulative_cost_rows(
            [
                {"start": 1767222000.0, "sum": 10.0},
                {"start": 1767225600.0, "sum": 10.5},
            ],
            0.6257,
        )
        self.assertEqual([6.257, 6.56985], [row["sum"] for row in rows])
        self.assertEqual(rows[1]["sum"], rows[1]["state"])
        self.assertEqual(timezone.utc, rows[0]["start"].tzinfo)
        self.assertEqual(
            datetime.fromtimestamp(1767222000.0, timezone.utc), rows[0]["start"]
        )

    def test_ignores_rows_without_a_finite_sum(self) -> None:
        rows = cost.cumulative_cost_rows(
            [
                {"start": 1.0, "sum": None},
                {"start": 2.0, "sum": float("nan")},
                {"start": 3.0, "sum": 2.0},
            ],
            1.5,
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(3.0, rows[0]["sum"])

    def test_prices_each_hour_with_the_rate_at_interval_start(self) -> None:
        rows = cost.hourly_cumulative_cost_rows(
            [
                {"start": 1767222000.0, "sum": 10.0},
                {"start": 1767225600.0, "sum": 11.0},
                {"start": 1767229200.0, "sum": 13.0},
            ],
            lambda at: 0.5 if at.hour == 23 else 1.0,
        )
        self.assertEqual([0.0, 0.5, 2.5], [row["sum"] for row in rows])
        self.assertEqual(
            [
                datetime.fromtimestamp(1767222000.0, timezone.utc),
                datetime.fromtimestamp(1767225600.0, timezone.utc),
                datetime.fromtimestamp(1767229200.0, timezone.utc),
            ],
            [row["start"] for row in rows],
        )

    def test_rejects_non_hourly_consumption_gap(self) -> None:
        with self.assertRaisesRegex(ValueError, "lukę"):
            cost.hourly_cumulative_cost_rows(
                [
                    {"start": 0.0, "sum": 10.0},
                    {"start": 7200.0, "sum": 11.0},
                ],
                lambda _at: 1.0,
            )

    def test_rejects_reset_of_cumulative_consumption(self) -> None:
        with self.assertRaisesRegex(ValueError, "wyzerowana"):
            cost.hourly_cumulative_cost_rows(
                [
                    {"start": 0.0, "sum": 10.0},
                    {"start": 3600.0, "sum": 1.0},
                ],
                lambda _at: 1.0,
            )


class CompensationStatisticIdTests(unittest.TestCase):
    def test_generated_statistic_id_is_lowercase(self) -> None:
        self.assertEqual(
            "polish_energy_price:01kysan_compensation_rcem",
            cost.compensation_statistic_id("01KYSAN"),
        )


class IntervalCumulativeValueRowsTests(unittest.TestCase):
    def test_prices_each_interval_with_the_rate_at_the_later_rows_start(self) -> None:
        rows = [
            {"start": 1767222000.0, "sum": 10.0},
            {"start": 1767225600.0, "sum": 11.0},
            {"start": 1767229200.0, "sum": 13.0},
        ]
        prices = {1767225600.0: 0.5, 1767229200.0: 1.0}
        result = cost.interval_cumulative_value_rows(
            rows, lambda at: prices[at.timestamp()]
        )
        self.assertEqual([0.0, 0.5, 2.5], [row["sum"] for row in result])

    def test_month_boundary_at_warsaw_midnight_in_winter(self) -> None:
        # UTC+1: local 23:00-24:00 on 31 Dec is still December, local
        # 00:00-01:00 on 1 Jan is already January.
        def price_at(at: datetime) -> float:
            return 0.1 if at.astimezone(WARSAW).month == 12 else 0.5

        rows = [
            {"start": datetime(2025, 12, 31, 21, tzinfo=timezone.utc).timestamp(), "sum": 0.0},
            {"start": datetime(2025, 12, 31, 22, tzinfo=timezone.utc).timestamp(), "sum": 1.0},
            {"start": datetime(2025, 12, 31, 23, tzinfo=timezone.utc).timestamp(), "sum": 3.0},
        ]
        result = cost.interval_cumulative_value_rows(rows, price_at)
        self.assertEqual([0.0, 0.1, 1.1], [row["sum"] for row in result])

    def test_month_boundary_at_warsaw_midnight_in_summer(self) -> None:
        # UTC+2: local 23:00-24:00 on 31 May is still May, local 00:00-01:00
        # on 1 Jun is already June.
        def price_at(at: datetime) -> float:
            return 0.2 if at.astimezone(WARSAW).month == 5 else 0.7

        rows = [
            {"start": datetime(2026, 5, 31, 20, tzinfo=timezone.utc).timestamp(), "sum": 0.0},
            {"start": datetime(2026, 5, 31, 21, tzinfo=timezone.utc).timestamp(), "sum": 1.0},
            {"start": datetime(2026, 5, 31, 22, tzinfo=timezone.utc).timestamp(), "sum": 3.0},
        ]
        result = cost.interval_cumulative_value_rows(rows, price_at)
        self.assertEqual([0.0, 0.2, 1.6], [row["sum"] for row in result])

    def test_gap_of_several_hours_is_not_an_error(self) -> None:
        rows = [
            {"start": 0.0, "sum": 0.0},
            {"start": 4 * 3600.0, "sum": 5.0},
        ]
        result = cost.interval_cumulative_value_rows(rows, lambda _at: 2.0)
        self.assertEqual([0.0, 10.0], [row["sum"] for row in result])

    def test_rejects_reset_of_cumulative_sum(self) -> None:
        with self.assertRaisesRegex(ValueError, "wyzerowana"):
            cost.interval_cumulative_value_rows(
                [
                    {"start": 0.0, "sum": 10.0},
                    {"start": 3600.0, "sum": 1.0},
                ],
                lambda _at: 1.0,
            )

    def test_missing_price_at_positive_delta_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "nieprawidłowa"):
            cost.interval_cumulative_value_rows(
                [
                    {"start": 0.0, "sum": 0.0},
                    {"start": 3600.0, "sum": 1.0},
                ],
                lambda _at: None,
            )

    def test_zero_price_is_valid(self) -> None:
        result = cost.interval_cumulative_value_rows(
            [
                {"start": 0.0, "sum": 0.0},
                {"start": 3600.0, "sum": 5.0},
            ],
            lambda _at: 0.0,
        )
        self.assertEqual([0.0, 0.0], [row["sum"] for row in result])

    def test_ignores_rows_without_a_finite_sum(self) -> None:
        result = cost.interval_cumulative_value_rows(
            [
                {"start": 1.0, "sum": None},
                {"start": 2.0, "sum": float("nan")},
                {"start": 3.0, "sum": 2.0},
            ],
            lambda _at: 1.0,
        )
        self.assertEqual(1, len(result))
        self.assertEqual(0.0, result[0]["sum"])
        self.assertEqual(
            datetime.fromtimestamp(3.0, timezone.utc), result[0]["start"]
        )


class RcemHistoryRepricingTests(unittest.TestCase):
    """End to end: interval_cumulative_value_rows repriced via export_price_at.

    The same four energy rows (10 kWh exported on 15 April 2026, split
    across three hourly intervals) are repriced with three different RCEm
    tables, reproducing the core behavior: history is not fixed until the
    settlement price is final, and a later correction reprices it again.
    """

    ROWS = [
        {"start": datetime(2026, 4, 15, 8, tzinfo=timezone.utc).timestamp(), "sum": 0.0},
        {"start": datetime(2026, 4, 15, 9, tzinfo=timezone.utc).timestamp(), "sum": 3.0},
        {"start": datetime(2026, 4, 15, 10, tzinfo=timezone.utc).timestamp(), "sum": 7.0},
        {"start": datetime(2026, 4, 15, 11, tzinfo=timezone.utc).timestamp(), "sum": 10.0},
    ]

    def _total(self, rcem_prices: dict[str, float]) -> float:
        def price_at(at: datetime) -> float | None:
            priced = export_price_at(
                at, settlement="rcem", rcem_prices=rcem_prices, correction=1.23
            )
            return priced.value if priced else None

        result = cost.interval_cumulative_value_rows(self.ROWS, price_at)
        return result[-1]["sum"]

    def test_provisional_march_price_before_april_is_published(self) -> None:
        self.assertAlmostEqual(
            round(10 * 0.30 * 1.23, 6), self._total({"2026-03": 0.30})
        )

    def test_repriced_once_april_is_published(self) -> None:
        self.assertAlmostEqual(
            round(10 * 0.25 * 1.23, 6),
            self._total({"2026-03": 0.30, "2026-04": 0.25}),
        )

    def test_repriced_again_once_april_is_corrected(self) -> None:
        self.assertAlmostEqual(
            round(10 * 0.26 * 1.23, 6),
            self._total({"2026-03": 0.30, "2026-04": 0.26}),
        )


if __name__ == "__main__":
    unittest.main()
