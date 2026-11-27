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
    opening_from_invoice,
    opening_tranches,
    parse_tranches,
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
        result = opening_tranches(100.0, "2025-05", self.inflows)
        self.assertEqual([("2025-05", 100.0)], [(t.month, t.remaining) for t in result])

    def test_balance_month_tranche_is_included(self) -> None:
        result = opening_tranches(50.0, "2025-04", self.inflows)
        self.assertEqual([("2025-04", 50.0)], [(t.month, t.remaining) for t in result])

    def test_boundary_tranche_is_partial_and_result_is_oldest_first(self) -> None:
        result = opening_tranches(200.0, "2025-05", self.inflows)
        self.assertEqual(
            [("2025-04", 77.0), ("2025-05", 123.0)],
            [(t.month, round(t.remaining, 6)) for t in result],
        )
        self.assertAlmostEqual(123.0, result[0].value)

    def test_balance_above_the_tranches_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            opening_tranches(246.5, "2025-05", self.inflows)

    def test_twelve_months_are_spread_and_the_thirteenth_is_not(self) -> None:
        old = {"2024-06": inflow("2024-06", 100, 1.0)}
        with self.assertRaises(ValueError):
            opening_tranches(10.0, "2025-06", old)
        self.assertEqual(1, len(opening_tranches(10.0, "2025-05", old)))


PGE = """2025-07-31 120,24
2025-08-31 141,80
2025-09-30 99,43
2025-10-31 95,17
2025-11-30 60,75
2025-12-31 41,85
2026-01-31 57,03
2026-02-28 80,06
"""


class ParseTranchesTests(unittest.TestCase):
    def test_pge_invoice_table(self) -> None:
        result = parse_tranches(PGE)
        self.assertEqual(8, len(result))
        self.assertEqual(120.24, result["2025-07"])
        self.assertAlmostEqual(696.33, sum(result.values()))

    def test_amount_formats_and_separators(self) -> None:
        self.assertEqual({"2025-07": 1448.11}, parse_tranches("2025-07-31 1 448,11 zł"))
        self.assertEqual(
            {"2025-07": 1448.11}, parse_tranches("2025-07-31\t1\u00a0448,11 PLN")
        )
        self.assertEqual({"2025-07": 120.24}, parse_tranches("2025-07;120.24"))
        self.assertEqual({"2025-07": 120.24}, parse_tranches("2025-07-31 | 120,24"))

    def test_whole_invoice_rows(self) -> None:
        rows = (
            "Całodobowa 2025-07-31 kWh 466 0,28483 163,26 0,00 43,02 120,24\n"
            "Całodobowa\t2025-06-30\tkWh\t774\t0,13630\t129,76\t0,00\t129,76\t0,00\n"
            "Całodobowa 2025-08-31 kWh 5 537 0,21468 1 141,80 0,00 0,00 1 141,80"
        )
        self.assertEqual(
            {"2025-07": 120.24, "2025-08": 1141.80}, parse_tranches(rows)
        )

    def test_empty_and_zero(self) -> None:
        self.assertEqual({}, parse_tranches(""))
        self.assertEqual({}, parse_tranches("\n  \n"))
        self.assertEqual({"2025-08": 5.0}, parse_tranches("2025-07 0,00\n2025-08 5"))

    def test_errors_name_the_line(self) -> None:
        for text, line in (
            ("2025-07 1\n2025-07-31 2", "Wiersz 2"),
            ("2025-07 1\n2025-13 2", "Wiersz 2"),
            ("2025-02-30 2", "Wiersz 1"),
            ("2025-07 -1", "Wiersz 1"),
            ("2025-07 abc", "Wiersz 1"),
            ("2025-07", "Wiersz 1"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, line):
                parse_tranches(text)


class InvoiceOpeningTests(unittest.TestCase):
    def test_energy_value_from_inflows_with_lower_bound_fallback(self) -> None:
        inflows = {"2025-07": inflow("2025-07", 100, 1.0)}
        result = opening_from_invoice({"2025-08": 123.0, "2025-07": 50.0}, inflows)
        self.assertEqual(["2025-07", "2025-08"], [t.month for t in result])
        self.assertEqual(
            (50.0, 50.0, 100.0),
            (result[0].value, result[0].remaining, result[0].energy_value),
        )
        self.assertAlmostEqual(100.0, result[1].energy_value)  # 123 / 1.23
        self.assertEqual(123.0, result[1].remaining)

    def test_pge_scenario_cost_comes_from_the_oldest_and_it_expires_in_2026_07(
        self,
    ) -> None:
        opening = opening_from_invoice(parse_tranches(PGE), {})
        result = simulate(opening, {}, {"2026-03": 100.0}, "2026-03", "2026-03")
        self.assertEqual(
            [("2025-07", 20.24), ("2025-08", 141.8)],
            [(t.month, round(t.remaining, 2)) for t in result][:2],
        )
        self.assertAlmostEqual(696.33 - 100.0, sum(t.remaining for t in result))
        alive = simulate(opening, {}, {}, "2026-03", "2026-06")
        self.assertEqual("2025-07", alive[0].month)
        alive = simulate(opening, {}, {}, "2026-03", "2026-07")
        self.assertEqual("2025-08", alive[0].month)

    def test_balance_month_inflow_is_not_counted_twice(self) -> None:
        inflows = {
            "2025-06": inflow("2025-06", 100, 1.0),
            "2025-07": inflow("2025-07", 100, 1.0),
        }
        opening = opening_tranches(123.0, "2025-07", inflows)
        result = simulate(opening, inflows, {}, "2025-08", "2025-08")
        self.assertAlmostEqual(123.0, sum(t.remaining for t in result))


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
