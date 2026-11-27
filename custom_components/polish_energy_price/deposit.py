"""Dependency-free model of the prosumer deposit under RCEm net-billing.

Monthly step: at the end of month C the cost of C is covered FIFO from
tranches assigned up to the end of C, the tranche for the energy of C is
assigned (it is booked on the 1st of C+1, so it never pays for C), and
tranches whose last settlement month (energy month + 12) has passed expire.
Months are ``"YYYY-MM"`` strings, which sort chronologically.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace

COEFFICIENT = 1.23
COEFFICIENT_FROM = "2025-02"
SETTLEMENT_MONTHS = 12
REFUND_LIMIT = 0.2
TOLERANCE = 0.005


@dataclass(slots=True)
class Tranche:
    """Funds assigned for the energy exported in one month."""

    month: str
    value: float
    remaining: float
    energy_value: float


def add_months(month: str, count: int) -> str:
    """Shift a ``"YYYY-MM"`` month by ``count`` months."""

    index = int(month[:4]) * 12 + int(month[5:7]) - 1 + count
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_range(first: str, last: str) -> Iterator[str]:
    """Yield every month from ``first`` to ``last`` inclusive."""

    while first <= last:
        yield first
        first = add_months(first, 1)


def coefficient(month: str) -> float:
    """Statutory deposit multiplier for energy exported in ``month``."""

    return COEFFICIENT if month >= COEFFICIENT_FROM else 1.0


def expires_after(tranche: Tranche) -> str:
    """Last month whose cost the tranche can still cover."""

    return add_months(tranche.month, SETTLEMENT_MONTHS)


def expiry_refund(tranche: Tranche) -> float:
    """Refund paid on expiry: the rest, capped at 20% of the energy value."""

    return min(tranche.remaining, REFUND_LIMIT * tranche.energy_value)


def inflow(month: str, kwh: float, rcem: float) -> Tranche:
    """Tranche for ``kwh`` exported in ``month`` at the RCEm of that month."""

    energy_value = kwh * rcem
    value = energy_value * coefficient(month)
    return Tranche(month, value, value, energy_value)


def monthly_inflows(
    kwh: Mapping[str, float], rcem: Mapping[str, float]
) -> dict[str, Tranche]:
    """Tranches for every month with exported energy."""

    result: dict[str, Tranche] = {}
    for month, energy in kwh.items():
        if energy <= 0:
            continue
        if month not in rcem:
            raise ValueError(f"Brak ceny RCEm za {month}")
        result[month] = inflow(month, energy, rcem[month])
    return result


def opening_tranches(
    balance: float, month: str, inflows: Mapping[str, Tranche]
) -> list[Tranche]:
    """Split the balance at the end of ``month`` into tranches, oldest first.

    The balance is taken from the newest tranche backwards; the boundary
    tranche is taken partially. Tranche ``month - 12`` has used its last
    settlement month by then, so only the 11 months before ``month`` count.
    """

    left = balance
    result: list[Tranche] = []
    for back in range(1, SETTLEMENT_MONTHS):
        tranche = inflows.get(add_months(month, -back))
        if tranche is None or left <= 0:
            continue
        taken = min(left, tranche.value)
        result.insert(0, replace(tranche, remaining=taken))
        left -= taken
    if left > TOLERANCE:
        raise ValueError(
            f"Saldo {balance:.2f} PLN jest większe niż wpływy za ostatnie "
            f"{SETTLEMENT_MONTHS - 1} miesięcy do {month} o {left:.2f} PLN"
        )
    return result


def simulate(
    opening: list[Tranche],
    inflows: Mapping[str, Tranche],
    costs: Mapping[str, float],
    first: str,
    last: str,
) -> list[Tranche]:
    """Run the monthly step for ``first``..``last`` and return live tranches.

    The balance is already after the cost of ``first``, so ``costs`` should
    start the month after; months missing from ``costs`` cost nothing. A cost
    above the balance is not carried over (the customer pays it).
    """

    tranches = [replace(tranche) for tranche in opening]
    for month in month_range(first, last):
        cost = costs.get(month, 0.0)
        for tranche in tranches:
            paid = min(tranche.remaining, cost)
            tranche.remaining -= paid
            cost -= paid
        if month in inflows:
            tranches.append(replace(inflows[month]))
        tranches = [
            tranche
            for tranche in tranches
            if tranche.remaining > 1e-9 and expires_after(tranche) > month
        ]
    return tranches
