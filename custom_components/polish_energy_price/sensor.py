"""Price sensor for Polish Energy Prices."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CUSTOM_PRICES,
    CONF_DAY_HOURS,
    CONF_METER_CLOCK,
    CONF_OPERATOR,
    CONF_PRICE_SOURCE,
    CONF_TARIFF,
    DOMAIN,
    METER_CLOCK_FIXED_WINTER,
    PRICE_SOURCE_CUSTOM,
    VALID_FROM,
    VALID_UNTIL,
)
from .coordinator import EnergyPriceCoordinator
from .tariff import (
    EXCISE_NET_PLN_KWH,
    OPERATOR_NAMES,
    SELLER_NAMES,
    VALID_YEAR,
    VAT,
    get_tariff,
    price_at,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the current gross electricity price sensor."""

    sensor = PolishEnergyPriceSensor(entry, entry.runtime_data)
    async_add_entities([sensor])
    entry.async_on_unload(
        async_track_time_change(hass, sensor.handle_hour_change, minute=0, second=0)
    )


class PolishEnergyPriceSensor(CoordinatorEntity[EnergyPriceCoordinator], SensorEntity):
    """Current all-in gross price of one kWh."""

    _attr_has_entity_name = True
    _attr_translation_key = "gross_price"
    _attr_icon = "mdi:cash-clock"
    _attr_native_unit_of_measurement = "PLN/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, coordinator: EnergyPriceCoordinator) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._operator = str(entry.data[CONF_OPERATOR])
        self._group = str(entry.data[CONF_TARIFF])
        self._tariff = get_tariff(self._operator, self._group)
        self._attr_unique_id = f"{entry.entry_id}_gross_price"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{OPERATOR_NAMES[self._operator]} {self._group}",
            manufacturer=OPERATOR_NAMES[self._operator],
            model=f"Taryfa {self._group} (2026)",
        )

    def _settings(self) -> dict[str, Any]:
        return {**self._entry.data, **self._entry.options}

    def _breakdown(self, now: datetime | None = None):
        settings = self._settings()
        custom = settings.get(CONF_CUSTOM_PRICES)
        if settings.get(CONF_PRICE_SOURCE) != PRICE_SOURCE_CUSTOM:
            custom = self.coordinator.data.prices
        return price_at(
            self._tariff,
            now or dt_util.now(),
            custom_energy=custom,
            day_hours=settings.get(CONF_DAY_HOURS),
            fixed_winter_time=settings.get(CONF_METER_CLOCK) == METER_CLOCK_FIXED_WINTER,
        )

    @property
    def available(self) -> bool:
        """Do not silently use expired annual tariffs."""

        return super().available and dt_util.now().year == VALID_YEAR

    @property
    def native_value(self) -> float | None:
        """Return current gross price in PLN/kWh."""

        if not self.available:
            return None
        return self._breakdown().total

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose an auditable split of the current price."""

        result = self._breakdown()
        settings = self._settings()
        custom = settings.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_CUSTOM
        attributes: dict[str, Any] = {
            "operator": OPERATOR_NAMES[self._operator],
            "tariff": self._group,
            "zone": result.zone_name,
            "zone_key": result.zone_key,
            "energy_price_gross": result.energy,
            "energy_price_includes_vat": True,
            "energy_price_includes_excise": True,
            "excise_duty_net": EXCISE_NET_PLN_KWH,
            "excise_duty_gross_component": result.excise,
            "energy_net_before_excise": round(
                max(0.0, result.energy / VAT - EXCISE_NET_PLN_KWH), 4
            ),
            "network_price_gross": result.network,
            "system_charges_gross": result.system,
            "distribution_price_gross": result.distribution,
            "price_source": "contract" if custom else self.coordinator.data.source,
            "energy_seller": "custom" if custom else SELLER_NAMES[self._operator],
            "fixed_monthly_fees_included": False,
            "valid_from": VALID_FROM,
            "valid_until": VALID_UNTIL,
        }
        if not custom:
            attributes.update(
                {
                    "ure_source_url": self.coordinator.data.source_url,
                    "ure_last_checked": self.coordinator.data.last_checked,
                    "ure_last_updated": self.coordinator.data.last_updated,
                    "ure_last_error": self.coordinator.data.error,
                    "distribution_price_source": "bundled_osd_tariff_2026",
                }
            )
        return attributes

    @callback
    def handle_hour_change(self, now: datetime) -> None:
        """Publish a state at every possible zone boundary."""

        self.async_write_ha_state()
