"""Tests for dependency-free cost statistics conversion."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
