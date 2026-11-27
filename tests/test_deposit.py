"""Tests for the dependency-free prosumer deposit model."""

from __future__ import annotations

import unittest

from custom_components.polish_energy_price.deposit import (
    Tranche,
    add_months,
    coefficient,
    expiry_refund,
    inflow,
    monthly_inflows,
    opening_tranches,
    simulate,
)


def tranche(month: str, remaining: float, energy_value: float = 100.0) -> Tranche:
    return Tranche(month, remaining, remaining, energy_value)


class CoefficientTests(unittest.TestCase):
    def test_coefficient_changes_with_energy_month_in_february_2025(self) -> None:
        self.assertEqual(1.0, coefficient("2025-01"))
        self.assertEqual(1.23, coefficient("2025-02"))
        self.assertEqual(100.0, inflow("2025-01", 100, 1.0).value)
        self.assertAlmostEqual(123.0, inflow("2025-02", 100, 1.0).value)

    def test_energy_value_has_no_coefficient(self) -> None:
        self.assertEqual(50.0, inflow("2025-03", 100, 0.5).energy_value)

    def test_add_months_crosses_year(self) -> None:
        self.assertEqual("2026-01", add_months("2025-11", 2))
        self.assertEqual("2024-12", add_months("2025-01", -1))

    def test_missing_price_fails_only_for_exported_energy(self) -> None:
        self.assertEqual({}, monthly_inflows({"2025-03": 0.0}, {}))
        with self.assertRaises(ValueError):
            monthly_inflows({"2025-03": 1.0}, {})


class OpeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.inflows = {
            month: inflow(month, 100, 1.0) for month in ("2025-04", "2025-05")
        }

    def test_balance_is_taken_from_the_newest_tranche_backwards(self) -> None:
        result = opening_tranches(100.0, "2025-06", self.inflows)
        self.assertEqual([("2025-05", 100.0)], [(t.month, t.remaining) for t in result])

    def test_boundary_tranche_is_partial_and_result_is_oldest_first(self) -> None:
        result = opening_tranches(200.0, "2025-06", self.inflows)
        self.assertEqual(
            [("2025-04", 77.0), ("2025-05", 123.0)],
            [(t.month, round(t.remaining, 6)) for t in result],
        )
        self.assertAlmostEqual(123.0, result[0].value)

    def test_balance_above_the_tranches_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            opening_tranches(246.5, "2025-06", self.inflows)

    def test_tranche_twelve_months_back_is_already_expired(self) -> None:
        old = {"2024-06": inflow("2024-06", 100, 1.0)}
        with self.assertRaises(ValueError):
            opening_tranches(10.0, "2025-06", old)
        self.assertEqual(1, len(opening_tranches(10.0, "2025-05", old)))


class SimulationTests(unittest.TestCase):
    def test_oldest_tranche_is_used_first(self) -> None:
        opening = [tranche("2025-03", 100), tranche("2025-04", 100)]
        result = simulate(opening, {}, {"2025-07": 130.0}, "2025-06", "2025-07")
        self.assertEqual([("2025-04", 70.0)], [(t.month, t.remaining) for t in result])

    def test_cost_above_balance_is_not_carried_over(self) -> None:
        result = simulate(
            [tranche("2025-03", 50)],
            {"2025-08": inflow("2025-08", 10, 1.0)},
            {"2025-07": 80.0, "2025-08": 5.0},
            "2025-06",
            "2025-08",
        )
        self.assertEqual(
            [("2025-08", 12.3)], [(t.month, round(t.remaining, 6)) for t in result]
        )

    def test_tranche_lives_through_month_plus_twelve_then_expires(self) -> None:
        opening = [tranche("2025-03", 100), tranche("2025-04", 50)]
        costs = {"2026-03": 10.0}
        alive = simulate(opening, {}, costs, "2026-02", "2026-02")
        self.assertEqual(2, len(alive))
        # The cost of 2026-03 still comes out of the expiring tranche.
        result = simulate(opening, {}, costs, "2026-02", "2026-03")
        self.assertEqual([("2025-04", 50.0)], [(t.month, t.remaining) for t in result])

    def test_refund_is_capped_at_twenty_percent_of_energy_value(self) -> None:
        self.assertEqual(20.0, expiry_refund(tranche("2025-03", 123.0)))
        self.assertEqual(7.0, expiry_refund(tranche("2025-03", 7.0)))

    def test_balance_month_tranche_is_added_but_current_month_is_not(self) -> None:
        inflows = {
            "2025-06": inflow("2025-06", 100, 1.0),
            "2025-08": inflow("2025-08", 100, 1.0),
        }
        result = simulate([], inflows, {"2025-07": 10.0}, "2025-06", "2025-07")
        self.assertEqual(
            [("2025-06", 113.0)], [(t.month, round(t.remaining, 6)) for t in result]
        )


if __name__ == "__main__":
    unittest.main()
