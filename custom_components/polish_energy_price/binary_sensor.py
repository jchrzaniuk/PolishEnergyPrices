"""Negative RCE export price flag for Polish Energy Prices (net-billing)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_EXPORT_SETTLEMENT,
    DOMAIN,
    EXPORT_SETTLEMENT_OFF,
    EXPORT_SETTLEMENT_RCE,
)
from .coordinator import EnergyPriceCoordinator
from .export_price import (
    ExportPrice,
    export_price_at,
    negative_periods,
    upcoming_export_periods,
)
from .tariff import WARSAW


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the negative RCE price flag; present only for the RCE settlement."""

    settings = {**entry.data, **entry.options}
    if settings.get(CONF_EXPORT_SETTLEMENT, EXPORT_SETTLEMENT_OFF) != EXPORT_SETTLEMENT_RCE:
        return

    entity = PolishEnergyExportNegativeBinarySensor(entry, entry.runtime_data.coordinator)
    async_add_entities([entity])

    @callback
    def handle_period_change(_now: datetime) -> None:
        entity.async_write_ha_state()

    entry.async_on_unload(
        async_track_time_change(
            hass, handle_period_change, minute=[0, 15, 30, 45], second=0
        )
    )


class PolishEnergyExportNegativeBinarySensor(
    CoordinatorEntity[EnergyPriceCoordinator], BinarySensorEntity
):
    """Whether the current RCE settlement period has a negative raw price."""

    _attr_has_entity_name = True
    _attr_name = "Ujemna cena RCE"
    _attr_icon = "mdi:transmission-tower-off"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, coordinator: EnergyPriceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_export_negative"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    def _price(self) -> ExportPrice | None:
        """Return the RCE price for the current settlement period."""

        return export_price_at(
            dt_util.now(),
            settlement=EXPORT_SETTLEMENT_RCE,
            rce_prices=self.coordinator.data.export_prices,
        )

    @property
    def available(self) -> bool:
        """Require both the coordinator and a price for the current period."""

        return super().available and self._price() is not None

    @property
    def is_on(self) -> bool:
        """Return True when the raw RCE price of the current period is negative."""

        price = self._price()
        return price.raw < 0 if price else False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw price, period and the upcoming negative periods."""

        data = self.coordinator.data
        price = self._price()
        upcoming = negative_periods(
            upcoming_export_periods(data.export_prices or {}, dt_util.now())
        )
        return {
            "Cena RCE netto [PLN/kWh]": price.raw if price else None,
            "Okres rozliczeniowy": (
                datetime.fromisoformat(price.period).astimezone(WARSAW).isoformat()
                if price
                else None
            ),
            "Liczba ujemnych okresów w prognozie": len(upcoming),
            "Najbliższy ujemny okres": upcoming[0]["start"] if upcoming else "Brak",
            "Źródło ceny": data.export_source_url,
            "Ostatni błąd": data.export_error or "Brak",
        }
