"""Dependency-free helpers for cumulative external cost statistics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import math
from typing import Iterable, Mapping, TypedDict


class SourceStatisticRow(TypedDict, total=False):
    """Subset of a Home Assistant statistics result used by the bridge."""

    start: float
    sum: float | None


class CostStatisticRow(TypedDict):
    """Recorder-compatible cumulative cost row."""

    start: datetime
    state: float
    sum: float


def cost_statistic_id(entry_id: str, zone: str) -> str:
    """Return an entity-ID-compatible external cost statistic ID."""

    return f"polish_energy_price:{entry_id}_cost_{zone}".lower()


def cumulative_cost_rows(
    rows: Iterable[Mapping[str, float | None]], price: float
) -> list[CostStatisticRow]:
    """Convert cumulative energy sums in kWh to cumulative gross costs in PLN."""

    result: list[CostStatisticRow] = []
    for row in rows:
        start = row.get("start")
        energy_sum = row.get("sum")
        if start is None or energy_sum is None:
            continue
        cost = float(energy_sum) * float(price)
        if not math.isfinite(cost):
            continue
        rounded = round(cost, 6)
        result.append(
            {
                "start": datetime.fromtimestamp(float(start), tz=timezone.utc),
                "state": rounded,
                "sum": rounded,
            }
        )
    return result


def hourly_cumulative_cost_rows(
    rows: Iterable[Mapping[str, float | None]],
    price_at: Callable[[datetime], float],
) -> list[CostStatisticRow]:
    """Price consecutive cumulative-energy rows using each hour's own rate.

    The first usable energy row is a zero-cost baseline. A difference between
    two consecutive sums represents consumption during the interval beginning
    at the timestamp of the earlier row.
    """

    result: list[CostStatisticRow] = []
    previous_start: datetime | None = None
    previous_sum: float | None = None
    running_cost = 0.0
    for row in rows:
        start = row.get("start")
        energy_sum = row.get("sum")
        if start is None or energy_sum is None:
            continue
        current_start = datetime.fromtimestamp(float(start), tz=timezone.utc)
        current_sum = float(energy_sum)
        if not math.isfinite(current_sum):
            continue
        if previous_start is None or previous_sum is None:
            previous_start = current_start
            previous_sum = current_sum
            result.append({"start": current_start, "state": 0.0, "sum": 0.0})
            continue

        seconds = (current_start - previous_start).total_seconds()
        if seconds <= 0:
            raise ValueError("Godziny statystyki zużycia nie są rosnące")
        delta = current_sum - previous_sum
        if delta < -1e-6:
            raise ValueError("Narastająca statystyka zużycia została wyzerowana")
        if seconds > 3900 and delta > 1e-9:
            raise ValueError(
                "Statystyka G13s ma lukę dłuższą niż jedna godzina"
            )
        if delta > 0:
            price = float(price_at(previous_start))
            if not math.isfinite(price) or price <= 0:
                raise ValueError("Cena godzinowa G13s jest nieprawidłowa")
            running_cost += delta * price
        rounded = round(running_cost, 6)
        result.append(
            {"start": current_start, "state": rounded, "sum": rounded}
        )
        previous_start = current_start
        previous_sum = current_sum
    return result
