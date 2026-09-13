"""Optional RCEm grid-export compensation statistic, repriced retroactively.

RCEm (the monthly settlement price for energy exported to the grid) is only
published on the 11th of the following month, and may later be corrected.
Home Assistant's Energy dashboard prices every kWh increment at whatever
price an entity reports *at that moment* and never reprices history, so
energy exported during month M would stay forever priced at an old rate.
This module rebuilds a cumulative PLN compensation statistic from a
cumulative "energy exported" statistic, repricing it whenever a new or
corrected RCEm becomes known.
"""

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
    CONF_EXPORT_CORRECTION,
    CONF_EXPORT_SETTLEMENT,
    CONF_EXPORT_STATISTIC,
    CONF_EXPORT_STATISTICS,
    CONF_OPERATOR,
    CONF_TARIFF,
    DEFAULT_EXPORT_CORRECTION,
    DOMAIN,
    EXPORT_SETTLEMENT_RCEM,
)
from .coordinator import EnergyPriceCoordinator
from .cost import compensation_statistic_id, interval_cumulative_value_rows
from .export_price import export_price_at, rcem_statistics_start, validate_rcem
from .tariff import OPERATOR_NAMES, get_tariff

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
LOOKBACK = timedelta(days=7)


class RcemCompensationStatisticsManager:
    """Generate a PLN compensation statistic from a cumulative export-energy statistic."""

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
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.compensation_statistics",
        )
        self._lock = asyncio.Lock()

    def _settings(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    async def async_refresh(self, _now: datetime | None = None) -> None:
        """Refresh the generated statistic without breaking price updates."""

        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._async_refresh()
            except (KeyError, TypeError, ValueError):
                _LOGGER.exception(
                    "Invalid RCEm compensation statistics configuration"
                )
            except Exception:  # noqa: BLE001 - recorder failures must not break pricing
                _LOGGER.exception("Could not refresh RCEm compensation statistics")

    async def _async_refresh(self) -> None:
        settings = self._settings()
        if settings.get(CONF_EXPORT_SETTLEMENT) != EXPORT_SETTLEMENT_RCEM:
            return
        if not settings.get(CONF_EXPORT_STATISTICS, False):
            return
        source_id = settings.get(CONF_EXPORT_STATISTIC)
        if not source_id:
            return
        source_id = str(source_id)

        stored = await self.store.async_load() or {}
        prices = {
            **validate_rcem(stored.get("prices")),
            **(self.coordinator.data.rcem_prices or {}),
        }
        if not prices:
            _LOGGER.warning(
                "Brak żadnej znanej ceny RCEm — statystyka zwrotu wstrzymana"
            )
            return

        source_changed = stored.get("source") != source_id
        start = (
            datetime.fromisoformat(str(stored["start"]))
            if not source_changed and stored.get("start")
            else rcem_statistics_start(prices)
        )
        if start is None:
            return

        recorder = get_instance(self.hass)
        metadata = await recorder.async_add_executor_job(
            list_statistic_ids, self.hass, {source_id}
        )
        by_id = {item["statistic_id"]: item for item in metadata}
        if source_id not in by_id:
            raise ValueError(f"Nie znaleziono statystyki energii: {source_id}")
        item = by_id[source_id]
        if item.get("unit_class") != EnergyConverter.UNIT_CLASS or not item.get(
            "has_sum"
        ):
            raise ValueError(
                f"Statystyka musi zawierać narastającą energię: {source_id}"
            )

        correction = float(
            settings.get(CONF_EXPORT_CORRECTION, DEFAULT_EXPORT_CORRECTION)
        )
        signature = json.dumps(
            {
                "source": source_id,
                "start": start.isoformat(),
                "correction": correction,
                "prices": prices,
            },
            sort_keys=True,
        )
        signature_changed = stored.get("signature") != signature

        # ponytail: every hourly refresh re-reads the whole source history
        # from its base row — the running compensation sum needs the full
        # history to reprice correctly whenever RCEm changes. Fine while
        # that history spans a few years; resume instead from the last
        # saved cumulative sum if it ever grows to spanning many years.
        source_rows = await recorder.async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                start,
                datetime.now(timezone.utc),
                {source_id},
                "hour",
                {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR},
                {"sum"},
            )
        )

        def price_at(at: datetime) -> float | None:
            priced = export_price_at(
                at, settlement="rcem", rcem_prices=prices, correction=correction
            )
            return priced.value if priced else None

        rows = interval_cumulative_value_rows(
            source_rows.get(source_id, []), price_at
        )
        if not rows:
            _LOGGER.warning(
                "Brak godzinowych danych energii oddanej dla %s", source_id
            )
            return

        target_id = compensation_statistic_id(self.entry.entry_id)
        if source_changed:
            recorder.async_clear_statistics([target_id])
        elif not signature_changed:
            last_saved = await recorder.async_add_executor_job(
                get_last_statistics, self.hass, 1, target_id, False, {"sum"}
            )
            saved_rows = last_saved.get(target_id, [])
            if saved_rows:
                cutoff = (
                    datetime.fromtimestamp(
                        float(saved_rows[-1]["start"]), tz=timezone.utc
                    )
                    - LOOKBACK
                )
                rows = [row for row in rows if row["start"] >= cutoff]
        if not rows:
            return

        async_add_external_statistics(
            self.hass,
            {
                "mean_type": StatisticMeanType.NONE,
                "has_sum": True,
                "name": (
                    f"{OPERATOR_NAMES[self.tariff.operator]} "
                    f"{self.tariff.group} — zwrot do sieci (RCEm)"
                ),
                "source": DOMAIN,
                "statistic_id": target_id,
                "unit_class": None,
                "unit_of_measurement": self.hass.config.currency,
            },
            rows,
        )

        await self.store.async_save(
            {
                "source": source_id,
                "start": start.isoformat(),
                "prices": prices,
                "signature": signature,
                "last_refresh": datetime.now(timezone.utc).isoformat(),
            }
        )
