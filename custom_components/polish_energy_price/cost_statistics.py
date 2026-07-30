"""Optional cost statistics bridge for external energy statistics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial
import json
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    CONF_CUSTOM_PRICES,
    CONF_EXTERNAL_STATISTICS,
    CONF_METER_CLOCK,
    CONF_OPERATOR,
    CONF_PRICE_SOURCE,
    CONF_TARIFF,
    DOMAIN,
    METER_CLOCK_FIXED_WINTER,
    PRICE_SOURCE_CUSTOM,
    external_statistic_key,
)
from .coordinator import EnergyPriceCoordinator
from .cost import cost_statistic_id, cumulative_cost_rows, hourly_cumulative_cost_rows
from .tariff import (
    OPERATOR_NAMES,
    ZONE_LABELS,
    WARSAW,
    get_tariff,
    price_at,
    price_for_zone,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
LOOKBACK = timedelta(days=7)


class ExternalCostStatisticsManager:
    """Generate PLN cost statistics from selected cumulative kWh statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: EnergyPriceCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.tariff = get_tariff(
            str(entry.data[CONF_OPERATOR]), str(entry.data[CONF_TARIFF])
        )
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cost_statistics"
        )
        self._lock = asyncio.Lock()

    def _settings(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def _is_hourly_g13s(self) -> bool:
        return self.tariff.operator == "tauron" and self.tariff.group.lower() == "g13s"

    def _mappings(self) -> dict[str, str]:
        settings = self._settings()
        if not settings.get(CONF_EXTERNAL_STATISTICS, False):
            return {}
        if self._is_hourly_g13s:
            key = external_statistic_key("g13s")
            return {"g13s": str(settings[key])} if settings.get(key) else {}
        return {
            zone: str(settings[external_statistic_key(zone)])
            for zone in self.tariff.zones
            if settings.get(external_statistic_key(zone))
        }

    def _prices(self) -> dict[str, float]:
        settings = self._settings()
        energy_prices = settings.get(CONF_CUSTOM_PRICES)
        if settings.get(CONF_PRICE_SOURCE) != PRICE_SOURCE_CUSTOM:
            energy_prices = self.coordinator.data.prices
        return {
            zone: price_for_zone(
                self.tariff,
                zone,
                custom_energy=energy_prices,
                distribution_net=self.coordinator.data.distribution_net,
                system_net=self.coordinator.data.system_total,
            ).total
            for zone in self.tariff.zones
        }

    async def async_refresh(self, _now: datetime | None = None) -> None:
        """Refresh generated statistics without making the integration unavailable."""

        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._async_refresh()
            except (KeyError, TypeError, ValueError):
                _LOGGER.exception("Invalid external cost statistics configuration")
            except Exception:  # noqa: BLE001 - recorder failures must not break pricing
                _LOGGER.exception("Could not refresh external cost statistics")

    async def _async_refresh(self) -> None:
        if not self.tariff.external_statistics_supported:
            return
        if not self._settings().get(CONF_EXTERNAL_STATISTICS, False):
            return
        mappings = self._mappings()
        target_zones = ("g13s",) if self._is_hourly_g13s else self.tariff.zones
        if set(mappings) != set(target_zones):
            _LOGGER.warning(
                "External cost statistics are enabled but not all zones are mapped"
            )
            return

        prices = self._prices()
        signature_data: dict[str, Any] = {
            "mappings": mappings,
            "prices": prices,
        }
        if self._is_hourly_g13s:
            signature_data["meter_clock"] = self._settings().get(CONF_METER_CLOCK)
        signature = json.dumps(signature_data, sort_keys=True)
        stored = await self.store.async_load() or {}
        full_refresh = stored.get("signature") != signature

        recorder = get_instance(self.hass)
        metadata = await recorder.async_add_executor_job(
            list_statistic_ids, self.hass, set(mappings.values())
        )
        by_id = {item["statistic_id"]: item for item in metadata}
        missing = set(mappings.values()) - set(by_id)
        if missing:
            raise ValueError(f"Nie znaleziono statystyk energii: {sorted(missing)}")
        invalid = [
            statistic_id
            for statistic_id, item in by_id.items()
            if item.get("unit_class") != EnergyConverter.UNIT_CLASS
            or not item.get("has_sum")
        ]
        if invalid:
            raise ValueError(
                f"Statystyki muszą zawierać narastającą energię: {sorted(invalid)}"
            )

        valid_from = datetime.fromisoformat(self.coordinator.data.valid_from).replace(
            tzinfo=WARSAW
        )
        start = valid_from.astimezone(timezone.utc)
        if not full_refresh:
            latest_starts: list[float] = []
            for zone in target_zones:
                result = await recorder.async_add_executor_job(
                    get_last_statistics,
                    self.hass,
                    1,
                    cost_statistic_id(self.entry.entry_id, zone),
                    False,
                    {"sum"},
                )
                rows = result.get(cost_statistic_id(self.entry.entry_id, zone), [])
                if not rows:
                    full_refresh = True
                    break
                latest_starts.append(float(rows[-1]["start"]))
            if not full_refresh and latest_starts:
                start = (
                    datetime.fromtimestamp(min(latest_starts), tz=timezone.utc)
                    - LOOKBACK
                )

        output_start = start
        # G13s must be recomputed from the first baseline row so the cumulative
        # cost at the seven-day overlap retains its correct historic offset.
        query_start = (
            valid_from.astimezone(timezone.utc) if self._is_hourly_g13s else start
        )
        source_rows = await recorder.async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                query_start,
                datetime.now(timezone.utc),
                set(mappings.values()),
                "hour",
                {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR},
                {"sum"},
            )
        )

        imported = 0
        if self._is_hourly_g13s:
            source_id = mappings["g13s"]
            settings = self._settings()
            energy_prices = settings.get(CONF_CUSTOM_PRICES)
            if settings.get(CONF_PRICE_SOURCE) != PRICE_SOURCE_CUSTOM:
                energy_prices = self.coordinator.data.prices

            def price_for_hour(at: datetime) -> float:
                return price_at(
                    self.tariff,
                    at,
                    custom_energy=energy_prices,
                    distribution_net=self.coordinator.data.distribution_net,
                    system_net=self.coordinator.data.system_total,
                    fixed_winter_time=settings.get(CONF_METER_CLOCK)
                    == METER_CLOCK_FIXED_WINTER,
                ).total

            rows = hourly_cumulative_cost_rows(
                source_rows.get(source_id, []), price_for_hour
            )
            if not full_refresh:
                rows = [row for row in rows if row["start"] >= output_start]
            if not rows:
                _LOGGER.warning("Brak godzinowych danych energii dla %s", source_id)
            else:
                target_id = cost_statistic_id(self.entry.entry_id, "g13s")
                async_add_external_statistics(
                    self.hass,
                    {
                        "mean_type": StatisticMeanType.NONE,
                        "has_sum": True,
                        "name": (
                            f"{OPERATOR_NAMES[self.tariff.operator]} "
                            f"{self.tariff.group} — koszt godzinowy"
                        ),
                        "source": DOMAIN,
                        "statistic_id": target_id,
                        "unit_class": None,
                        "unit_of_measurement": self.hass.config.currency,
                    },
                    rows,
                )
                imported += len(rows)

        if not self._is_hourly_g13s:
            for zone, source_id in mappings.items():
                rows = cumulative_cost_rows(
                    source_rows.get(source_id, []), prices[zone]
                )
                if not rows:
                    _LOGGER.warning("No energy rows available for %s", source_id)
                    continue
                target_id = cost_statistic_id(self.entry.entry_id, zone)
                async_add_external_statistics(
                    self.hass,
                    {
                        "mean_type": StatisticMeanType.NONE,
                        "has_sum": True,
                        "name": (
                            f"{OPERATOR_NAMES[self.tariff.operator]} "
                            f"{self.tariff.group} — koszt, {ZONE_LABELS[zone]}"
                        ),
                        "source": DOMAIN,
                        "statistic_id": target_id,
                        "unit_class": None,
                        "unit_of_measurement": self.hass.config.currency,
                    },
                    rows,
                )
                imported += len(rows)

        if imported:
            await self.store.async_save(
                {
                    "signature": signature,
                    "last_refresh": datetime.now(timezone.utc).isoformat(),
                }
            )
            _LOGGER.debug("Queued %d external cost statistic rows", imported)
