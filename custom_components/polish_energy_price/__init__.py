"""Polish Energy Prices integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import EnergyPriceCoordinator
    from .cost_statistics import ExternalCostStatisticsManager


@dataclass(slots=True)
class PolishEnergyPriceRuntimeData:
    """Runtime objects owned by one config entry."""

    coordinator: EnergyPriceCoordinator
    cost_statistics: ExternalCostStatisticsManager | None = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Polish Energy Prices from a config entry."""

    from datetime import timedelta

    from homeassistant.helpers.event import async_track_time_interval

    from .const import PLATFORMS
    from .coordinator import EnergyPriceCoordinator
    from .cost_statistics import ExternalCostStatisticsManager

    coordinator = EnergyPriceCoordinator(hass, entry)
    await coordinator.async_initialize()
    cost_statistics = ExternalCostStatisticsManager(hass, entry, coordinator)
    entry.runtime_data = PolishEnergyPriceRuntimeData(coordinator, cost_statistics)
    await cost_statistics.async_refresh()
    entry.async_on_unload(
        async_track_time_interval(
            hass, cost_statistics.async_refresh, timedelta(hours=1)
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    from .const import PLATFORMS

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
