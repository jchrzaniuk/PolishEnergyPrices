"""Platform-independent hourly price forecasts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol

from .tariff import (
    OPERATOR_NAMES,
    WARSAW,
    DynamicZoneUnavailable,
    TariffDefinition,
    price_at,
)

SCHEMA_VERSION = 1
DEFAULT_FORECAST_HOURS = 48
MAX_FORECAST_HOURS = 168
RESOLUTION_MIN = 60


class EnergyPriceDataLike(Protocol):
    """Price data consumed by the pure forecast generator."""

    prices: Mapping[str, float]
    source: str
    error: str | None
    distribution_net: Mapping[str, float] | None
    valid_from: str
    valid_until: str
    official_error: str | None
    dynamic_zones: Mapping[str, str] | None
    dynamic_error: str | None

    @property
    def system_total(self) -> float:
        """Return the total net variable system rate."""


@dataclass(frozen=True, slots=True)
class ForecastSlot:
    """Gross marginal price components for one absolute hour."""

    start: datetime
    end: datetime
    zone: str
    zone_name: str
    energy_gross: float
    network_gross: float
    system_gross: float
    distribution_gross: float
    price_gross: float


@dataclass(frozen=True, slots=True)
class PriceForecast:
    """One immutable forecast shared by all output adapters."""

    schema_version: int
    generated_at: datetime
    provider: str
    tariff_id: str
    operator_id: str
    tariff_code: str
    tariff_name: str
    currency: str
    unit: str
    resolution_min: int
    requested_hours: int
    complete: bool
    valid_from: str
    valid_until: str
    energy_source: str
    source_status: str
    slots: tuple[ForecastSlot, ...]


def build_forecast(
    tariff: TariffDefinition,
    data: EnergyPriceDataLike,
    generated_at: datetime,
    *,
    custom_energy: Mapping[str, float] | None = None,
    day_hours: str | None = None,
    fixed_winter_time: bool = False,
    hours: int = DEFAULT_FORECAST_HOURS,
) -> PriceForecast:
    """Build an hourly forecast from one already-loaded price snapshot."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("Czas wygenerowania prognozy musi zawierać strefę czasową")
    if isinstance(hours, bool) or not isinstance(hours, int):
        raise ValueError("Liczba godzin prognozy musi być liczbą całkowitą")
    if not 1 <= hours <= MAX_FORECAST_HOURS:
        raise ValueError(
            f"Liczba godzin prognozy musi mieścić się w zakresie "
            f"1-{MAX_FORECAST_HOURS}"
        )

    valid_from = _date_value(data.valid_from, "valid_from")
    valid_until = _date_value(data.valid_until, "valid_until")
    if valid_from > valid_until:
        raise ValueError("Początek ważności taryfy przypada po jej końcu")

    local_generated_at = generated_at.astimezone(WARSAW)
    local_start = local_generated_at.replace(minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(timezone.utc)
    valid_start = datetime.combine(valid_from, time.min, tzinfo=WARSAW)
    valid_end = datetime.combine(
        valid_until + timedelta(days=1), time.min, tzinfo=WARSAW
    )
    energy_prices = data.prices if custom_energy is None else custom_energy
    energy_source = data.source if custom_energy is None else "custom"
    source_status = _source_status(
        data,
        local_start,
        valid_start,
        valid_end,
        energy_source,
    )

    slots: list[ForecastSlot] = []
    for offset in range(hours):
        slot_start_utc = start_utc + timedelta(hours=offset)
        slot_end_utc = slot_start_utc + timedelta(hours=1)
        slot_start = slot_start_utc.astimezone(WARSAW)
        slot_end = slot_end_utc.astimezone(WARSAW)
        if slot_start < valid_start or slot_end > valid_end:
            break
        try:
            result = price_at(
                tariff,
                slot_start,
                custom_energy=energy_prices,
                distribution_net=data.distribution_net,
                system_net=data.system_total,
                day_hours=day_hours,
                fixed_winter_time=fixed_winter_time,
                dynamic_zones=data.dynamic_zones,
            )
        except DynamicZoneUnavailable:
            break
        slots.append(
            ForecastSlot(
                start=slot_start,
                end=slot_end,
                zone=result.zone_key,
                zone_name=result.zone_name,
                energy_gross=result.energy,
                network_gross=result.network,
                system_gross=result.system,
                distribution_gross=result.distribution,
                price_gross=result.total,
            )
        )

    return PriceForecast(
        schema_version=SCHEMA_VERSION,
        generated_at=local_generated_at,
        provider="polish_energy_price",
        tariff_id=f"{tariff.operator}:{tariff.group.lower()}",
        operator_id=tariff.operator,
        tariff_code=tariff.group,
        tariff_name=f"{OPERATOR_NAMES[tariff.operator]} {tariff.group}",
        currency="PLN",
        unit="PLN/kWh",
        resolution_min=RESOLUTION_MIN,
        requested_hours=hours,
        complete=len(slots) == hours,
        valid_from=data.valid_from,
        valid_until=data.valid_until,
        energy_source=energy_source,
        source_status=source_status,
        slots=tuple(slots),
    )


def resize_forecast(forecast: PriceForecast, hours: int) -> PriceForecast:
    """Return a shorter view of a forecast generated for a larger horizon."""

    if isinstance(hours, bool) or not isinstance(hours, int):
        raise ValueError("Liczba godzin prognozy musi być liczbą całkowitą")
    if not 1 <= hours <= forecast.requested_hours:
        raise ValueError(
            f"Liczba godzin prognozy musi mieścić się w zakresie "
            f"1-{forecast.requested_hours}"
        )
    slots = forecast.slots[:hours]
    return replace(
        forecast,
        requested_hours=hours,
        complete=len(slots) == hours,
        slots=slots,
    )


def forecast_to_dict(forecast: PriceForecast) -> dict[str, object]:
    """Serialize the common HTTP and MQTT forecast payload."""

    return {
        "schema_version": forecast.schema_version,
        "generated_at": forecast.generated_at.isoformat(),
        "provider": forecast.provider,
        "tariff_id": forecast.tariff_id,
        "operator_id": forecast.operator_id,
        "tariff_code": forecast.tariff_code,
        "tariff_name": forecast.tariff_name,
        "currency": forecast.currency,
        "unit": forecast.unit,
        "resolution_min": forecast.resolution_min,
        "requested_hours": forecast.requested_hours,
        "complete": forecast.complete,
        "valid_from": forecast.valid_from,
        "valid_until": forecast.valid_until,
        "energy_source": forecast.energy_source,
        "source_status": forecast.source_status,
        "slots": [slot_to_dict(slot) for slot in forecast.slots],
    }


def slot_to_dict(slot: ForecastSlot) -> dict[str, object]:
    """Serialize one complete forecast slot."""

    return {
        "start": slot.start.isoformat(),
        "end": slot.end.isoformat(),
        "zone": slot.zone,
        "zone_name": slot.zone_name,
        "energy_gross": slot.energy_gross,
        "network_gross": slot.network_gross,
        "system_gross": slot.system_gross,
        "distribution_gross": slot.distribution_gross,
        "price_gross": slot.price_gross,
    }


def forecast_attributes(forecast: PriceForecast) -> dict[str, object]:
    """Build the compact Home Assistant forecast attributes."""

    return {
        "provider": forecast.provider,
        "tariff_id": forecast.tariff_id,
        "operator_id": forecast.operator_id,
        "tariff_code": forecast.tariff_code,
        "tariff_name": forecast.tariff_name,
        "currency": forecast.currency,
        "unit": forecast.unit,
        "forecast": [
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "energy_gross": slot.energy_gross,
                "distribution_gross": slot.distribution_gross,
                "price_gross": slot.price_gross,
                "zone": slot.zone,
            }
            for slot in forecast.slots
        ],
        "forecast_generated_at": forecast.generated_at.isoformat(),
        "forecast_resolution_min": forecast.resolution_min,
        "forecast_complete": forecast.complete,
        "forecast_source_status": forecast.source_status,
        "forecast_valid_from": forecast.valid_from,
        "forecast_valid_until": forecast.valid_until,
    }


def _source_status(
    data: EnergyPriceDataLike,
    start: datetime,
    valid_start: datetime,
    valid_end: datetime,
    energy_source: str,
) -> str:
    if start < valid_start:
        return "not_yet_valid"
    if start >= valid_end:
        return "expired"
    if (
        energy_source.endswith("_cache")
        or data.error
        or data.official_error
        or data.dynamic_error
    ):
        return "cache_or_warning"
    return "current"


def _date_value(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"Nieprawidłowa data {field}: {value!r}") from err
