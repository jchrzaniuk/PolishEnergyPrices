"""Dependency-free model of the prosumer deposit under RCEm net-billing.

Monthly step: at the end of month C the cost of C is covered FIFO from
tranches assigned up to the end of C, the tranche for the energy of C is
assigned (it is booked on the 1st of C+1, so it never pays for C), and
tranches whose last settlement month (energy month + 12) has passed expire.
The starting balance (an invoice) already includes the tranche of its own
month D and is after the cost of D, so the simulation starts at D+1.
Months are ``"YYYY-MM"`` strings, which sort chronologically.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date
import re

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

    The balance includes the tranche for ``month`` itself. It is taken from
    the newest tranche backwards over ``month``..``month - 11`` (12 months);
    the boundary tranche is taken partially.
    """

    left = balance
    result: list[Tranche] = []
    for back in range(SETTLEMENT_MONTHS):
        tranche = inflows.get(add_months(month, -back))
        if tranche is None or left <= 0:
            continue
        taken = min(left, tranche.value)
        result.insert(0, replace(tranche, remaining=taken))
        left -= taken
    if left > TOLERANCE:
        raise ValueError(
            f"Saldo {balance:.2f} PLN jest większe niż wpływy za "
            f"{SETTLEMENT_MONTHS} miesięcy do {month} włącznie o {left:.2f} PLN"
        )
    return result


_TRANCHE_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?:-(\d{2}))?(?!\d)")
# The last amount on the line; a space joins only groups of three digits.
_TRANCHE_AMOUNT = re.compile(
    r"(?<![\d.,])(-?\d{1,3}(?: \d{3})*(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?)"
    r"\s*(?:zł|PLN)?\s*$",
    re.IGNORECASE,
)


def parse_tranches(text: str) -> dict[str, float]:
    """Parse invoice lines into ``{"YYYY-MM": amount}``.

    Each line holds the first date (``YYYY-MM-DD`` or ``YYYY-MM``) and the
    remaining amount as its last number, so a whole row copied from the
    invoice table works too. The amount may use a decimal comma or dot,
    spaces (also U+00A0) as thousands separator and a trailing ``zł``/``PLN``.
    Zero amounts are skipped.
    """

    result: dict[str, float] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.replace("\u00a0", " ").strip()
        if not line:
            continue
        found = _TRANCHE_DATE.search(line)
        amount_match = _TRANCHE_AMOUNT.search(line[found.end() :] if found else "")
        if found is None or amount_match is None:
            raise ValueError(f"Wiersz {number}: nieprawidłowa data lub kwota")
        year, mon, day = found.groups()
        amount_text = amount_match.group(1)
        try:
            date(int(year), int(mon), int(day or 1))
        except ValueError:
            raise ValueError(f"Wiersz {number}: nieprawidłowa data") from None
        amount = float(re.sub(r"\s", "", amount_text).replace(",", "."))
        if amount < 0:
            raise ValueError(f"Wiersz {number}: kwota nie może być ujemna")
        key = f"{year}-{mon}"
        if key in result:
            raise ValueError(f"Wiersz {number}: miesiąc {key} występuje drugi raz")
        if amount:
            result[key] = amount
    return result


def opening_from_invoice(
    remaining: Mapping[str, float], inflows: Mapping[str, Tranche]
) -> list[Tranche]:
    """Tranches read from the invoice table, oldest first.

    ``value`` and ``remaining`` are the invoice amount. The energy value (basis
    of the refund cap) comes from ``inflows``; for a month missing there it is
    ``remaining / coefficient``, a lower bound, so the estimated refund is
    understated by at most the remaining amount.
    """

    return [
        Tranche(
            month,
            amount,
            amount,
            inflows[month].energy_value
            if month in inflows
            else amount / coefficient(month),
        )
        for month, amount in sorted(remaining.items())
    ]


def simulate(
    opening: list[Tranche],
    inflows: Mapping[str, Tranche],
    costs: Mapping[str, float],
    first: str,
    last: str,
) -> list[Tranche]:
    """Run the monthly step for ``first``..``last`` and return live tranches.

    ``opening`` is the balance after the cost of the month before ``first``
    and already includes that month's tranche, so ``first`` is the month after
    the balance month. Months missing from ``costs`` cost nothing. A cost
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
