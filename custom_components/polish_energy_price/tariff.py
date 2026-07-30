"""Pure-Python model of Polish household electricity tariffs for 2026.

Energy prices are gross and include excise duty. Most come from regulated
default-seller tariffs; G13s uses the official TAURON commercial offer.
Distribution rates in ``TARIFFS`` are net; the calculator adds all variable
system charges and VAT. Fixed monthly fees are deliberately excluded because
they do not change the marginal cost of one kWh.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
VAT = 1.23
EXCISE_NET_PLN_KWH = 0.005
SYSTEM_NET_PLN_KWH = 0.0332 + 0.0073 + 0.0030
VALID_YEAR = 2026

OPERATOR_NAMES: dict[str, str] = {
    "tauron": "TAURON Dystrybucja S.A.",
    "pge": "PGE Dystrybucja S.A.",
    "energa": "ENERGA-OPERATOR S.A.",
    "stoen": "Stoen Operator Sp. z o.o.",
    "enea": "ENEA Operator Sp. z o.o.",
}

SELLER_NAMES: dict[str, str] = {
    "tauron": "TAURON Sprzedaż sp. z o.o.",
    "pge": "PGE Obrót S.A.",
    "energa": "ENERGA-OBRÓT S.A.",
    "stoen": "E.ON Polska S.A.",
    "enea": "ENEA S.A.",
}

ZONE_LABELS: dict[str, str] = {
    "calodobowa": "całodobowa",
    "dzienna": "dzienna",
    "nocna": "nocna",
    "szczytowa": "szczytowa",
    "pozaszczytowa": "pozaszczytowa",
    "szczyt_przedpoludniowy": "szczyt przedpołudniowy",
    "szczyt_popoludniowy": "szczyt popołudniowy",
    "pozostale": "pozostałe godziny",
}

G13S_PERIODS = (
    "zima_dzien_roboczy",
    "zima_dzien_wolny",
    "lato_dzien_roboczy",
    "lato_dzien_wolny",
)
G13S_BASE_ZONES = ("dzienna_pozaszczytowa", "dzienna_szczytowa", "nocna")
G13S_ZONES = tuple(
    f"{period}_{zone}" for period in G13S_PERIODS for zone in G13S_BASE_ZONES
)
_G13S_PERIOD_LABELS = {
    "zima_dzien_roboczy": "zima, dzień roboczy",
    "zima_dzien_wolny": "zima, dzień wolny",
    "lato_dzien_roboczy": "lato, dzień roboczy",
    "lato_dzien_wolny": "lato, dzień wolny",
}
_G13S_ZONE_LABELS = {
    "dzienna_pozaszczytowa": "dzienna pozaszczytowa",
    "dzienna_szczytowa": "dzienna szczytowa",
    "nocna": "nocna",
}
ZONE_LABELS.update(
    {
        f"{period}_{zone}": (
            f"{_G13S_ZONE_LABELS[zone]} — {_G13S_PERIOD_LABELS[period]}"
        )
        for period in G13S_PERIODS
        for zone in G13S_BASE_ZONES
    }
)


@dataclass(frozen=True, slots=True)
class Season:
    """Clock windows used in one part of the year."""

    start: tuple[int, int]
    end: tuple[int, int]
    hours: Mapping[str, tuple[tuple[int, int], ...]]


@dataclass(frozen=True, slots=True)
class TariffDefinition:
    """All variable prices and switching rules for one OSD tariff group."""

    operator: str
    group: str
    zones: tuple[str, ...]
    seasons: tuple[Season, ...]
    distribution_net: Mapping[str, float]
    energy_gross: Mapping[str, float]
    off_peak_zone: str | None = None
    off_peak_days: str = "weekend_holiday"
    description: str = ""
    external_statistics_supported: bool = True


@dataclass(frozen=True, slots=True)
class PriceBreakdown:
    """Gross price components for one instant."""

    zone_key: str
    zone_name: str
    energy: float
    excise: float
    network: float
    system: float
    distribution: float
    total: float


def _season(
    hours: Mapping[str, Sequence[Sequence[int]]],
    start: tuple[int, int] = (1, 1),
    end: tuple[int, int] = (12, 31),
) -> Season:
    return Season(
        start,
        end,
        {
            key: tuple((int(a), int(b)) for a, b in windows)
            for key, windows in hours.items()
        },
    )


ALL_DAY = (_season({"calodobowa": ((0, 24),)}),)
STANDARD_DAY_NIGHT = (
    _season({"dzienna": ((6, 13), (15, 22)), "nocna": ((13, 15), (22, 6))}),
)
PGE_SEASONAL_DAY_NIGHT = (
    _season(
        {"dzienna": ((6, 15), (17, 22)), "nocna": ((15, 17), (22, 6))},
        (4, 1),
        (9, 30),
    ),
    _season(
        {"dzienna": ((6, 13), (15, 22)), "nocna": ((13, 15), (22, 6))},
        (10, 1),
        (3, 31),
    ),
)
TAURON_G13_SEASONS = (
    _season(
        {
            "szczyt_przedpoludniowy": ((7, 13),),
            "szczyt_popoludniowy": ((19, 22),),
            "pozostale": ((13, 19), (22, 7)),
        },
        (4, 1),
        (9, 30),
    ),
    _season(
        {
            "szczyt_przedpoludniowy": ((7, 13),),
            "szczyt_popoludniowy": ((16, 21),),
            "pozostale": ((13, 16), (21, 7)),
        },
        (10, 1),
        (3, 31),
    ),
)
TAURON_G13S_SEASONS = (
    _season(
        {
            "dzienna_pozaszczytowa": ((9, 17),),
            "dzienna_szczytowa": ((7, 9), (17, 21)),
            "nocna": ((21, 7),),
        },
        (4, 1),
        (9, 30),
    ),
    _season(
        {
            "dzienna_pozaszczytowa": ((10, 15),),
            "dzienna_szczytowa": ((7, 10), (15, 21)),
            "nocna": ((21, 7),),
        },
        (10, 1),
        (3, 31),
    ),
)


def _tariff(
    operator: str,
    group: str,
    zones: tuple[str, ...],
    seasons: tuple[Season, ...],
    distribution: Mapping[str, float],
    energy: Mapping[str, float],
    *,
    off_peak_zone: str | None = None,
    off_peak_days: str = "weekend_holiday",
    description: str = "",
    external_statistics_supported: bool = True,
) -> TariffDefinition:
    return TariffDefinition(
        operator,
        group,
        zones,
        seasons,
        distribution,
        energy,
        off_peak_zone,
        off_peak_days,
        description,
        external_statistics_supported,
    )


TARIFFS: dict[tuple[str, str], TariffDefinition] = {
    ("tauron", "G11"): _tariff(
        "tauron",
        "G11",
        ("calodobowa",),
        ALL_DAY,
        {"calodobowa": 0.2464},
        {"calodobowa": 0.6175},
        description="jednostrefowa",
    ),
    ("tauron", "G12"): _tariff(
        "tauron",
        "G12",
        ("dzienna", "nocna"),
        STANDARD_DAY_NIGHT,
        {"dzienna": 0.2841, "nocna": 0.0558},
        {"dzienna": 0.6740, "nocna": 0.5141},
        description="dwustrefowa dzień/noc",
    ),
    ("tauron", "G12w"): _tariff(
        "tauron",
        "G12w",
        ("szczytowa", "pozaszczytowa"),
        (
            _season(
                {"szczytowa": ((6, 13), (15, 22)), "pozaszczytowa": ((13, 15), (22, 6))}
            ),
        ),
        {"szczytowa": 0.3298, "pozaszczytowa": 0.0512},
        {"szczytowa": 0.7712, "pozaszczytowa": 0.5141},
        off_peak_zone="pozaszczytowa",
        description="weekendowa",
    ),
    ("tauron", "G13"): _tariff(
        "tauron",
        "G13",
        ("szczyt_przedpoludniowy", "szczyt_popoludniowy", "pozostale"),
        TAURON_G13_SEASONS,
        {
            "szczyt_przedpoludniowy": 0.2203,
            "szczyt_popoludniowy": 0.3898,
            "pozostale": 0.0392,
        },
        {
            "szczyt_przedpoludniowy": 0.5803,
            "szczyt_popoludniowy": 0.9631,
            "pozostale": 0.5240,
        },
        off_peak_zone="pozostale",
        description="trójstrefowa sezonowa",
    ),
    ("tauron", "G13s"): _tariff(
        "tauron",
        "G13s",
        G13S_ZONES,
        TAURON_G13S_SEASONS,
        {
            "zima_dzien_roboczy_dzienna_pozaszczytowa": 0.1999,
            "zima_dzien_roboczy_dzienna_szczytowa": 0.3332,
            "zima_dzien_roboczy_nocna": 0.1094,
            "zima_dzien_wolny_dzienna_pozaszczytowa": 0.1200,
            "zima_dzien_wolny_dzienna_szczytowa": 0.1960,
            "zima_dzien_wolny_nocna": 0.1094,
            "lato_dzien_roboczy_dzienna_pozaszczytowa": 0.1000,
            "lato_dzien_roboczy_dzienna_szczytowa": 0.2842,
            "lato_dzien_roboczy_nocna": 0.1094,
            "lato_dzien_wolny_dzienna_pozaszczytowa": 0.0400,
            "lato_dzien_wolny_dzienna_szczytowa": 0.1176,
            "lato_dzien_wolny_nocna": 0.1094,
        },
        {
            "zima_dzien_roboczy_dzienna_pozaszczytowa": 0.6827,
            "zima_dzien_roboczy_dzienna_szczytowa": 0.8723,
            "zima_dzien_roboczy_nocna": 0.6089,
            "zima_dzien_wolny_dzienna_pozaszczytowa": 0.4121,
            "zima_dzien_wolny_dzienna_szczytowa": 0.5258,
            "zima_dzien_wolny_nocna": 0.6089,
            "lato_dzien_roboczy_dzienna_pozaszczytowa": 0.3383,
            "lato_dzien_roboczy_dzienna_szczytowa": 0.8723,
            "lato_dzien_roboczy_nocna": 0.6212,
            "lato_dzien_wolny_dzienna_pozaszczytowa": 0.1390,
            "lato_dzien_wolny_dzienna_szczytowa": 0.3526,
            "lato_dzien_wolny_nocna": 0.6212,
        },
        description="wielostrefowa sezonowa Tanie Godziny",
    ),
    ("pge", "G11"): _tariff(
        "pge",
        "G11",
        ("calodobowa",),
        ALL_DAY,
        {"calodobowa": 0.3469},
        {"calodobowa": 0.6189},
        description="jednostrefowa",
    ),
    ("pge", "G12"): _tariff(
        "pge",
        "G12",
        ("dzienna", "nocna"),
        PGE_SEASONAL_DAY_NIGHT,
        {"dzienna": 0.4014, "nocna": 0.0765},
        {"dzienna": 0.7018, "nocna": 0.4635},
        description="dwustrefowa sezonowa",
    ),
    ("pge", "G12w"): _tariff(
        "pge",
        "G12w",
        ("dzienna", "nocna"),
        PGE_SEASONAL_DAY_NIGHT,
        {"dzienna": 0.4276, "nocna": 0.0845},
        {"dzienna": 0.7221, "nocna": 0.5271},
        off_peak_zone="nocna",
        description="weekendowa sezonowa",
    ),
    ("pge", "G12n"): _tariff(
        "pge",
        "G12n",
        ("dzienna", "nocna"),
        (_season({"dzienna": ((5, 1),), "nocna": ((1, 5),)}),),
        {"dzienna": 0.3470, "nocna": 0.0347},
        {"dzienna": 0.6840, "nocna": 0.4873},
        off_peak_zone="nocna",
        off_peak_days="sunday_holiday",
        description="nocna 1:00–5:00; niedziele i święta całodobowo",
    ),
    ("energa", "G11"): _tariff(
        "energa",
        "G11",
        ("calodobowa",),
        ALL_DAY,
        {"calodobowa": 0.3485},
        {"calodobowa": 0.6172},
        description="jednostrefowa",
    ),
    ("energa", "G12"): _tariff(
        "energa",
        "G12",
        ("dzienna", "nocna"),
        STANDARD_DAY_NIGHT,
        {"dzienna": 0.3844, "nocna": 0.0827},
        {"dzienna": 0.7182, "nocna": 0.4678},
        description="dwustrefowa dzień/noc",
    ),
    ("energa", "G12w"): _tariff(
        "energa",
        "G12w",
        ("dzienna", "nocna"),
        STANDARD_DAY_NIGHT,
        {"dzienna": 0.4017, "nocna": 0.0851},
        {"dzienna": 0.7512, "nocna": 0.4908},
        off_peak_zone="nocna",
        description="weekendowa",
    ),
    ("energa", "G12r"): _tariff(
        "energa",
        "G12r",
        ("szczytowa", "pozaszczytowa"),
        (
            _season(
                {"szczytowa": ((7, 13), (16, 22)), "pozaszczytowa": ((13, 16), (22, 7))}
            ),
        ),
        {"szczytowa": 0.3640, "pozaszczytowa": 0.0882},
        {"szczytowa": 0.8262, "pozaszczytowa": 0.3772},
        description="Ekonomiczna Dolina",
    ),
    ("stoen", "G11"): _tariff(
        "stoen",
        "G11",
        ("calodobowa",),
        ALL_DAY,
        {"calodobowa": 0.2342},
        {"calodobowa": 0.6212},
        description="jednostrefowa",
    ),
    ("stoen", "G12"): _tariff(
        "stoen",
        "G12",
        ("dzienna", "nocna"),
        STANDARD_DAY_NIGHT,
        {"dzienna": 0.2545, "nocna": 0.0555},
        {"dzienna": 0.6635, "nocna": 0.5283},
        description="dwustrefowa dzień/noc",
    ),
    ("stoen", "G12w"): _tariff(
        "stoen",
        "G12w",
        ("szczytowa", "pozaszczytowa"),
        (_season({"szczytowa": ((6, 22),), "pozaszczytowa": ((22, 6),)}),),
        {"szczytowa": 0.2570, "pozaszczytowa": 0.1079},
        {"szczytowa": 0.6512, "pozaszczytowa": 0.5467},
        off_peak_zone="pozaszczytowa",
        description="weekendowa",
    ),
    ("stoen", "G12as"): _tariff(
        "stoen",
        "G12as",
        ("dzienna", "nocna"),
        (_season({"dzienna": ((6, 22),), "nocna": ((22, 6),)}),),
        {"dzienna": 0.2342, "nocna": 0.2342},
        {"dzienna": 0.6758, "nocna": 0.5344},
        description="antysmogowa; bez rozliczania nadwyżki nocnej",
    ),
    ("enea", "G11"): _tariff(
        "enea",
        "G11",
        ("calodobowa",),
        ALL_DAY,
        {"calodobowa": 0.2456},
        {"calodobowa": 0.6187},
        description="jednostrefowa",
    ),
    ("enea", "G12"): _tariff(
        "enea",
        "G12",
        ("dzienna", "nocna"),
        STANDARD_DAY_NIGHT,
        {"dzienna": 0.2779, "nocna": 0.0913},
        {"dzienna": 0.7170, "nocna": 0.4205},
        description="dwustrefowa; godziny można nadpisać z umowy",
    ),
    ("enea", "G12w"): _tariff(
        "enea",
        "G12w",
        ("szczytowa", "pozaszczytowa"),
        (_season({"szczytowa": ((6, 21),), "pozaszczytowa": ((21, 6),)}),),
        {"szczytowa": 0.2702, "pozaszczytowa": 0.0813},
        {"szczytowa": 0.8079, "pozaszczytowa": 0.4323},
        off_peak_zone="pozaszczytowa",
        description="weekendowa",
    ),
}


def groups_for(operator: str) -> list[str]:
    """Return only groups with complete regulated energy prices."""

    return [group for op, group in TARIFFS if op == operator]


def get_tariff(operator: str, group: str) -> TariffDefinition:
    """Return a tariff definition or raise an actionable error."""

    operator_key = operator.lower()
    group_key = group.lower()
    for (candidate_operator, candidate_group), definition in TARIFFS.items():
        if candidate_operator == operator_key and candidate_group.lower() == group_key:
            return definition
    raise ValueError(f"Nieobsługiwana taryfa: {operator}:{group}")


def parse_day_hours(value: str) -> tuple[tuple[int, int], ...]:
    """Parse e.g. ``6-13,15-22`` and validate a non-overlapping set of hours."""

    windows: list[tuple[int, int]] = []
    try:
        for raw in value.split(","):
            start_text, end_text = raw.strip().split("-", 1)
            start, end = int(start_text), int(end_text)
            if not 0 <= start <= 23 or not 1 <= end <= 24 or start == end:
                raise ValueError
            windows.append((start, end))
    except (TypeError, ValueError) as err:
        raise ValueError("Użyj formatu 6-13,15-22 (pełne godziny 0–24).") from err
    if not windows:
        raise ValueError("Podaj co najmniej jeden przedział godzin.")
    covered = [hour for hour in range(24) if _covers(windows, hour)]
    expected = sum((end - start) % 24 for start, end in windows)
    if len(covered) != expected:
        raise ValueError("Przedziały godzin nie mogą na siebie nachodzić.")
    return tuple(windows)


def _complement(windows: Sequence[Sequence[int]]) -> tuple[tuple[int, int], ...]:
    free = [hour for hour in range(24) if not _covers(windows, hour)]
    result: list[list[int]] = []
    for hour in free:
        if result and result[-1][1] == hour:
            result[-1][1] = hour + 1
        else:
            result.append([hour, hour + 1])
    if len(result) > 1 and result[0][0] == 0 and result[-1][1] == 24:
        result[-1][1] = result[0][1]
        result.pop(0)
    return tuple((start, end) for start, end in result)


def _covers(windows: Sequence[Sequence[int]], hour: int) -> bool:
    for start, end in windows:
        if start < end and start <= hour < end:
            return True
        if start > end and (hour >= start or hour < end):
            return True
    return False


def _in_season(day: date, season: Season) -> bool:
    key = (day.month, day.day)
    if season.start <= season.end:
        return season.start <= key <= season.end
    return key >= season.start or key <= season.end


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=16)
def polish_holidays(year: int) -> frozenset[date]:
    """Polish statutory holidays, including Christmas Eve since 2025."""

    easter = _easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),
            date(year, 1, 6),
            date(year, 5, 1),
            date(year, 5, 3),
            date(year, 8, 15),
            date(year, 11, 1),
            date(year, 11, 11),
            date(year, 12, 24),
            date(year, 12, 25),
            date(year, 12, 26),
            easter,
            easter + timedelta(days=1),
            easter + timedelta(days=49),
            easter + timedelta(days=60),
        }
    )


def _is_off_peak_day(day: date, mode: str) -> bool:
    holiday = day in polish_holidays(day.year)
    if mode == "sunday_holiday":
        return day.weekday() == 6 or holiday
    return day.weekday() >= 5 or holiday


def _billing_time(ts: datetime, fixed_winter_time: bool) -> datetime:
    local = ts.astimezone(WARSAW)
    if fixed_winter_time and local.dst() and local.dst() != timedelta(0):
        return local - timedelta(hours=1)
    return local


def zone_at(
    tariff: TariffDefinition,
    ts: datetime,
    *,
    day_hours: str | None = None,
    fixed_winter_time: bool = False,
) -> str:
    """Resolve the billing zone active at an aware timestamp."""

    local = _billing_time(ts, fixed_winter_time)
    if tariff.operator == "tauron" and tariff.group.lower() == "g13s":
        season_name = "lato" if 4 <= local.month <= 9 else "zima"
        day_name = (
            "dzien_wolny"
            if _is_off_peak_day(local.date(), "weekend_holiday")
            else "dzien_roboczy"
        )
        season = next(item for item in tariff.seasons if _in_season(local.date(), item))
        for base_zone, windows in season.hours.items():
            if _covers(windows, local.hour):
                return f"{season_name}_{day_name}_{base_zone}"
        raise RuntimeError(f"Brak strefy G13s dla {local.isoformat()}")

    if tariff.off_peak_zone and _is_off_peak_day(local.date(), tariff.off_peak_days):
        return tariff.off_peak_zone

    if day_hours and tariff.operator == "enea" and tariff.group == "G12":
        day_windows = parse_day_hours(day_hours)
        hours = {"dzienna": day_windows, "nocna": _complement(day_windows)}
    else:
        season = next(item for item in tariff.seasons if _in_season(local.date(), item))
        hours = season.hours

    for zone, windows in hours.items():
        if _covers(windows, local.hour):
            return zone
    raise RuntimeError(
        f"Brak strefy dla {tariff.operator}:{tariff.group} {local.isoformat()}"
    )


def price_at(
    tariff: TariffDefinition,
    ts: datetime,
    *,
    custom_energy: Mapping[str, float] | None = None,
    distribution_net: Mapping[str, float] | None = None,
    system_net: float | None = None,
    day_hours: str | None = None,
    fixed_winter_time: bool = False,
) -> PriceBreakdown:
    """Calculate the gross marginal price of one kWh at ``ts``."""

    zone = zone_at(
        tariff,
        ts,
        day_hours=day_hours,
        fixed_winter_time=fixed_winter_time,
    )
    return price_for_zone(
        tariff,
        zone,
        custom_energy=custom_energy,
        distribution_net=distribution_net,
        system_net=system_net,
    )


def price_for_zone(
    tariff: TariffDefinition,
    zone: str,
    *,
    custom_energy: Mapping[str, float] | None = None,
    distribution_net: Mapping[str, float] | None = None,
    system_net: float | None = None,
) -> PriceBreakdown:
    """Calculate the gross marginal price for one explicit tariff zone."""

    if zone not in tariff.zones:
        raise ValueError(
            f"Nieznana strefa {zone!r} taryfy {tariff.operator}:{tariff.group}"
        )
    prices = custom_energy if custom_energy else tariff.energy_gross
    energy = round(float(prices[zone]), 4)
    network_rates = distribution_net or tariff.distribution_net
    system_rate = SYSTEM_NET_PLN_KWH if system_net is None else float(system_net)
    network = round(float(network_rates[zone]) * VAT, 4)
    system = round(system_rate * VAT, 4)
    distribution = round((float(network_rates[zone]) + system_rate) * VAT, 4)
    return PriceBreakdown(
        zone_key=zone,
        zone_name=ZONE_LABELS[zone],
        energy=energy,
        # Energy prices supplied by URE and accepted in the config flow are
        # already gross.  Expose the duty explicitly, but never add it twice.
        excise=round(EXCISE_NET_PLN_KWH * VAT, 4),
        network=network,
        system=system,
        distribution=distribution,
        total=round(energy + distribution, 4),
    )
