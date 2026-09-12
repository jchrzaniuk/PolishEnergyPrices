"""Ceny energii wprowadzonej do sieci (net-billing): RCE i RCEm z PSE.

RCE (rynkowa cena energii) jest publikowana w okresach 15-minutowych, dzień
przed dobą handlową. RCEm (rynkowa miesięczna cena energii) jest publikowana
11. dnia następnego miesiąca, z możliwą późniejszą korektą. Obie ceny są
netto — bez VAT i akcyzy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
import json
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .tariff import WARSAW

RCE_API_URL = "https://api.raporty.pse.pl/api/rce-pln"
RCEM_PAGE = "https://www.pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej"

_MONTHS: Mapping[str, int] = {
    "styczeń": 1,
    "luty": 2,
    "marzec": 3,
    "kwiecień": 4,
    "maj": 5,
    "czerwiec": 6,
    "lipiec": 7,
    "sierpień": 8,
    "wrzesień": 9,
    "październik": 10,
    "listopad": 11,
    "grudzień": 12,
}


@dataclass(frozen=True, slots=True)
class RceSnapshot:
    """Ceny RCE dla okresów 15-minutowych i najnowsza publikacja PSE."""

    prices: dict[str, float]
    publication_utc: str | None


@dataclass(frozen=True, slots=True)
class ExportPrice:
    """Cena energii wprowadzonej do sieci dla jednego okresu rozliczeniowego."""

    settlement: str
    period: str
    raw: float
    value: float


def build_rce_url(day: date, base_url: str = RCE_API_URL) -> str:
    """Zbuduj zapytanie OData o ceny RCE dla jednej doby handlowej."""

    query = urlencode(
        {
            "$filter": f"business_date eq '{day.isoformat()}'",
            "$first": "500",
        },
        quote_via=quote,
    )
    return f"{base_url}?{query}"


def parse_rce(payload: bytes | str) -> RceSnapshot:
    """Parsuj ceny RCE PSE na klucze UTC początku okresu 15-minutowego."""

    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as err:
        raise ValueError("Odpowiedź RCE PSE nie jest prawidłowym JSON") from err
    rows = document.get("value") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Odpowiedź RCE PSE nie zawiera listy value")

    prices: dict[str, float] = {}
    publications: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Wiersz RCE PSE nie jest obiektem")

        rce_pln = row.get("rce_pln")
        if isinstance(rce_pln, bool) or not isinstance(rce_pln, (int, float)):
            raise ValueError("RCE PSE zwróciło nieprawidłową cenę rce_pln")
        price = round(rce_pln / 1000, 6)
        if not -5.0 <= price <= 5.0:
            raise ValueError(f"Cena RCE poza bezpiecznym zakresem: {price}")

        end = _pse_utc_datetime(row.get("dtime_utc"))
        length = _period_minutes(row.get("period_utc"))
        key = rce_period_key(end - timedelta(minutes=length))

        previous = prices.setdefault(key, price)
        if previous != price:
            raise ValueError(f"RCE PSE zwróciło dwie różne ceny dla okresu {key}")

        publication = row.get("publication_ts_utc")
        if publication:
            publications.append(_pse_utc_datetime(publication))

    newest = max(publications).isoformat() if publications else None
    return RceSnapshot(prices, newest)


def rce_period_key(ts: datetime) -> str:
    """Zwróć klucz UTC początku okresu 15-minutowego zawierającego ``ts``."""

    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("Czas okresu RCE musi zawierać strefę czasową")
    utc = ts.astimezone(timezone.utc)
    floored_minute = (utc.minute // 15) * 15
    return utc.replace(minute=floored_minute, second=0, microsecond=0).isoformat()


def validate_export_prices(raw: object) -> dict[str, float]:
    """Zwaliduj mapę cen RCE wczytaną z pamięci podręcznej."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("Ceny RCE w pamięci podręcznej nie są obiektem")
    result: dict[str, float] = {}
    for raw_key, raw_value in raw.items():
        key = rce_period_key(datetime.fromisoformat(str(raw_key)))
        value = float(raw_value)
        if not -5.0 <= value <= 5.0:
            raise ValueError(f"Cena RCE w pamięci podręcznej poza zakresem: {value}")
        result[key] = value
    return result


def parse_rcem(page: str, year: int) -> dict[str, float]:
    """Parsuj tabelę RCEm PSE dla jednego roku na mapę ``"YYYY-MM" -> PLN/kWh``."""

    parser = _TableParser()
    parser.feed(page)

    table = None
    for candidate in parser.tables:
        if (
            candidate
            and len(candidate[0]) == 1
            and candidate[0][0]["tag"] == "th"
            and _cell_text(candidate[0][0]) == str(year)
        ):
            table = candidate
            break
    if table is None:
        raise ValueError(f"Strona RCEm nie zawiera tabeli dla roku {year}")

    base: dict[int, float] = {}
    corrected: dict[int, float] = {}
    month: int | None = None
    for row in table:
        if len(row) == 1 and row[0]["tag"] == "td" and row[0]["colspan"] == "4":
            name = _cell_text(row[0]).rstrip("*").lower()
            month = _MONTHS.get(name)
            continue
        if month is None or not row or row[0]["tag"] != "td":
            continue
        label = _cell_text(row[0]).lower()
        value_text = _cell_text(row[1]) if len(row) > 1 else ""
        value = _rcem_value(value_text)
        if value is None:
            continue
        if label.startswith("skorygowana rcem"):
            corrected[month] = value
        elif label.startswith("rcem"):
            base[month] = value

    result: dict[str, float] = {}
    for month_number, raw_value in {**base, **corrected}.items():
        price = round(raw_value / 1000, 6)
        if not 0 <= price <= 3:
            raise ValueError(f"Cena RCEm poza bezpiecznym zakresem: {price}")
        result[f"{year:04d}-{month_number:02d}"] = price
    if not result:
        raise ValueError(f"Strona RCEm nie zawiera cen dla roku {year}")
    return result


def validate_rcem(raw: object) -> dict[str, float]:
    """Zwaliduj mapę cen RCEm wczytaną z pamięci podręcznej."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("Ceny RCEm w pamięci podręcznej nie są obiektem")
    result: dict[str, float] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key)
        if len(key) != 7:
            raise ValueError(f"Nieprawidłowy klucz miesiąca RCEm: {key}")
        try:
            datetime.strptime(key, "%Y-%m")
        except ValueError as err:
            raise ValueError(f"Nieprawidłowy klucz miesiąca RCEm: {key}") from err
        value = float(raw_value)
        if not 0 <= value <= 3:
            raise ValueError(f"Cena RCEm w pamięci podręcznej poza zakresem: {value}")
        result[key] = value
    return result


def rcem_month_key(day: date) -> str:
    """Zwróć klucz miesiąca RCEm odpowiadający danej dacie kalendarzowej."""

    return f"{day.year:04d}-{day.month:02d}"


def export_price_at(
    ts: datetime,
    *,
    settlement: str,
    rce_prices: Mapping[str, float] | None = None,
    rcem_prices: Mapping[str, float] | None = None,
    correction: float = 1.0,
) -> ExportPrice | None:
    """Zwróć cenę energii wprowadzonej do sieci dla wskazanej chwili."""

    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("Czas rozliczenia eksportu musi zawierać strefę czasową")

    if settlement == "rce":
        period = rce_period_key(ts)
        raw = (rce_prices or {}).get(period)
    elif settlement == "rcem":
        period = _last_known_month(ts, rcem_prices or {})
        raw = (rcem_prices or {}).get(period) if period else None
    else:
        raise ValueError(f"Nieznany tryb rozliczenia eksportu: {settlement}")

    if raw is None:
        return None
    # Ustawa o OZE: ujemna RCE nie pomniejsza depozytu prosumenckiego — do
    # rozliczenia przyjmuje się wtedy zero. "raw" zostaje ceną surową PSE,
    # także ujemną; przycięciu podlega tylko "value".
    value = round(max(raw, 0.0) * correction, 6)
    return ExportPrice(settlement, period, raw, value)


def _last_known_month(ts: datetime, rcem_prices: Mapping[str, float]) -> str | None:
    target = rcem_month_key(ts.astimezone(WARSAW).date())
    candidates = [key for key in rcem_prices if key <= target]
    return max(candidates) if candidates else None


def _rcem_value(text: str) -> float | None:
    if text in ("", "-"):
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError as err:
        raise ValueError(f"Nieprawidłowa wartość ceny RCEm: {text!r}") from err


class _TableParser(HTMLParser):
    """Zbiera zawartość wszystkich tabel HTML jako wiersze komórek."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict[str, str]]]] = []
        self._table: list[list[dict[str, str]]] | None = None
        self._row: list[dict[str, str]] | None = None
        self._cell: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            values = dict(attrs)
            self._cell = {"tag": tag, "colspan": values.get("colspan") or "", "text": ""}

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def _cell_text(cell: dict[str, str]) -> str:
    return " ".join(cell["text"].split())


def _pse_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("RCE PSE nie podało czasu UTC okresu")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as err:
        raise ValueError(f"Nieprawidłowy czas UTC RCE PSE: {value!r}") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _period_minutes(period_utc: object) -> int:
    if not isinstance(period_utc, str) or not period_utc.strip():
        raise ValueError("RCE PSE nie podało długości okresu (period_utc)")
    parts = period_utc.split(" - ")
    if len(parts) != 2:
        raise ValueError(f"Nieprawidłowy format okresu RCE PSE: {period_utc!r}")
    start = _minutes_of_day(parts[0], period_utc)
    end = _minutes_of_day(parts[1], period_utc)
    length = end - start
    if length <= 0:
        length += 24 * 60
    if length not in (15, 60):
        raise ValueError(f"Nieobsługiwana długość okresu RCE PSE: {period_utc!r}")
    return length


def upcoming_export_periods(
    prices: Mapping[str, float],
    now: datetime,
    *,
    correction: float = 1.0,
    limit: int = 192,
) -> list[dict[str, Any]]:
    """Zwróć ceny RCE dla okresów od bieżącego w przód, najwyżej ``limit``.

    Okresy są posortowane rosnąco. Każdy wpis zawiera początek okresu w
    czasie ``Europe/Warsaw``, cenę rozliczeniową po przycięciu ujemnych cen
    do zera i przemnożeniu przez ``correction`` (tak jak ``export_price_at``)
    oraz surową cenę RCE.
    """

    current = rce_period_key(now)
    upcoming = sorted(key for key in prices if key >= current)
    return [
        {
            "start": datetime.fromisoformat(key).astimezone(WARSAW).isoformat(),
            "cena_netto": round(max(prices[key], 0.0) * correction, 6),
            "rce_netto": prices[key],
        }
        for key in upcoming[:limit]
    ]


def _minutes_of_day(value: str, context: str) -> int:
    try:
        hour_str, minute_str = value.split(":")
        hour, minute = int(hour_str), int(minute_str)
    except ValueError as err:
        raise ValueError(f"Nieprawidłowy format okresu RCE PSE: {context!r}") from err
    if not (0 <= hour <= 24 and 0 <= minute < 60 and (hour < 24 or minute == 0)):
        raise ValueError(f"Nieprawidłowy format okresu RCE PSE: {context!r}")
    return hour * 60 + minute
