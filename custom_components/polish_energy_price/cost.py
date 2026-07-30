"""Dependency-free helpers for cumulative external cost statistics."""

from __future__ import annotations

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
