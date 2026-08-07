"""Energetyczny Kompas PSE as the zone source for TAURON G14dynamic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from typing import Mapping
from urllib.parse import quote, urlencode

from .tariff import WARSAW, dynamic_hour_key

KOMPAS_API_URL = "https://api.raporty.pse.pl/api/pdgsz"
LEVEL_TO_ZONE: Mapping[int, str] = {
    0: "S1_zalecane_uzytkowanie",
    1: "S2_normalne",
    2: "S3_zalecane_oszczedzanie",
    3: "S4_wymagane_ograniczenie",
}


@dataclass(frozen=True, slots=True)
class KompasSnapshot:
    """Active hourly zones and the newest PSE publication timestamp."""

    zones: dict[str, str]
    publication_utc: str | None


def build_kompas_url(day: date, base_url: str = KOMPAS_API_URL) -> str:
    """Build an OData query for the active revision of one trading day."""

    query = urlencode(
        {
            "$filter": (
                f"business_date eq '{day.isoformat()}' and is_active eq true"
            ),
            "$first": "500",
        },
        quote_via=quote,
    )
    return f"{base_url}?{query}"


def parse_kompas(payload: bytes | str) -> KompasSnapshot:
    """Parse active PSE rows into UTC hour keys used by the tariff model."""

    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as err:
        raise ValueError("Odpowiedź Kompasu PSE nie jest prawidłowym JSON") from err
    rows = document.get("value") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Odpowiedź Kompasu PSE nie zawiera listy value")

    zones: dict[str, str] = {}
    publications: list[datetime] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("is_active") is not True:
            continue
        level = row.get("usage_fcst")
        if isinstance(level, bool) or not isinstance(level, int):
            raise ValueError("Kompas PSE zwrócił nieprawidłowy poziom zalecenia")
        try:
            zone = LEVEL_TO_ZONE[level]
        except KeyError as err:
            raise ValueError(
                f"Kompas PSE zwrócił nieznany poziom zalecenia: {level}"
            ) from err

        instant = _pse_utc_datetime(row.get("dtime_utc"))
        key = dynamic_hour_key(instant)
        previous = zones.setdefault(key, zone)
        if previous != zone:
            raise ValueError(f"Kompas PSE zwrócił dwie strefy dla godziny {key}")

        publication = row.get("publication_ts_utc")
        if publication:
            publications.append(_pse_utc_datetime(publication))

    newest = max(publications).isoformat() if publications else None
    return KompasSnapshot(zones, newest)


def validate_dynamic_zones(raw: object) -> dict[str, str]:
    """Validate a cached mapping without accepting unknown zone names."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("Strefy Kompasu w pamięci podręcznej nie są obiektem")
    allowed = set(LEVEL_TO_ZONE.values())
    result: dict[str, str] = {}
    for raw_key, raw_zone in raw.items():
        key = dynamic_hour_key(datetime.fromisoformat(str(raw_key)))
        zone = str(raw_zone)
        if zone not in allowed:
            raise ValueError(f"Nieznana strefa Kompasu w pamięci: {zone}")
        result[key] = zone
    return result


def validate_dynamic_publications(raw: object) -> dict[str, str]:
    """Validate a cached mapping of trading-day publication timestamps."""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(
            "Znaczniki publikacji Kompasu w pamięci podręcznej nie są obiektem"
        )
    result: dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        key = date.fromisoformat(str(raw_key)).isoformat()
        result[key] = str(raw_value)
    return result


def local_day_for_key(key: str) -> date:
    """Return the Warsaw calendar date represented by a UTC hour key."""

    return datetime.fromisoformat(key).astimezone(WARSAW).date()


def _pse_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Kompas PSE nie podał czasu UTC godziny")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as err:
        raise ValueError(f"Nieprawidłowy czas UTC Kompasu PSE: {value!r}") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
