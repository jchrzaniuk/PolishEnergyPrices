"""Price sensor for Polish Energy Prices."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
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
)
from .coordinator import EnergyPriceCoordinator
from .tariff import (
    EXCISE_NET_PLN_KWH,
    OPERATOR_NAMES,
    SELLER_NAMES,
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

    sensor = PolishEnergyPriceSensor(entry, entry.runtime_data.coordinator)
    component_sensors = [
        PolishEnergyPriceComponentSensor(
            entry,
            entry.runtime_data.coordinator,
            sensor,
            key,
            name,
            icon,
        )
        for key, name, icon in PRICE_COMPONENTS
    ]
    entities = [sensor, *component_sensors]
    async_add_entities(entities)

    @callback
    def handle_hour_change(_now: datetime) -> None:
        for entity in entities:
            entity.async_write_ha_state()

    entry.async_on_unload(
        async_track_time_change(hass, handle_hour_change, minute=0, second=0)
    )


PRICE_COMPONENTS = (
    ("energia_czynna_brutto", "Energia czynna brutto", "mdi:lightning-bolt"),
    ("skladnik_sieciowy_brutto", "Składnik sieciowy brutto", "mdi:transmission-tower"),
    ("oplata_jakosciowa_brutto", "Opłata jakościowa brutto", "mdi:sine-wave"),
    ("oplata_oze_brutto", "Opłata OZE brutto", "mdi:solar-power"),
    (
        "oplata_kogeneracyjna_brutto",
        "Opłata kogeneracyjna brutto",
        "mdi:heat-wave",
    ),
    ("dystrybucja_brutto", "Dystrybucja brutto", "mdi:transmission-tower-export"),
    ("akcyza_netto", "Akcyza netto", "mdi:bank"),
    ("vat_lacznie", "VAT łącznie", "mdi:percent"),
    ("cena_laczna_netto", "Cena łączna netto", "mdi:cash-minus"),
)


class PolishEnergyPriceSensor(CoordinatorEntity[EnergyPriceCoordinator], SensorEntity):
    """Current all-in gross price of one kWh."""

    _attr_has_entity_name = True
    # Keep the UI consistently Polish regardless of the browser language.
    # The unique ID stays unchanged, so existing Energy dashboard mappings do
    # not need to be recreated.
    _attr_name = "Cena energii brutto"
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
            model=(f"Taryfa {self._group} ({self.coordinator.data.valid_from[:4]})"),
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
            distribution_net=self.coordinator.data.distribution_net,
            system_net=self.coordinator.data.system_total,
            day_hours=settings.get(CONF_DAY_HOURS),
            fixed_winter_time=settings.get(CONF_METER_CLOCK)
            == METER_CLOCK_FIXED_WINTER,
        )

    @property
    def available(self) -> bool:
        """Do not silently use expired annual tariffs."""

        return super().available and self.coordinator.data.is_valid_on(
            dt_util.now().date()
        )

    @property
    def native_value(self) -> float | None:
        """Return current gross price in PLN/kWh."""

        if not self.available:
            return None
        return self._breakdown().total

    def price_components(self) -> dict[str, float]:
        """Return numeric components shared by attributes and child sensors."""

        result = self._breakdown()
        system_rates = self.coordinator.data.system_net or {}
        network_net = float(
            (self.coordinator.data.distribution_net or self._tariff.distribution_net)[
                result.zone_key
            ]
        )
        quality_net = float(system_rates.get("quality", 0))
        oze_net = float(system_rates.get("oze", 0))
        cogeneration_net = float(system_rates.get("cogeneration", 0))
        system_net = quality_net + oze_net + cogeneration_net
        distribution_net = network_net + system_net
        energy_net_with_excise = result.energy / VAT
        return {
            "energia_czynna_brutto": round(result.energy, 4),
            "skladnik_sieciowy_brutto": round(network_net * VAT, 4),
            "oplata_jakosciowa_brutto": round(quality_net * VAT, 4),
            "oplata_oze_brutto": round(oze_net * VAT, 4),
            "oplata_kogeneracyjna_brutto": round(cogeneration_net * VAT, 4),
            "dystrybucja_brutto": round(result.distribution, 4),
            "akcyza_netto": EXCISE_NET_PLN_KWH,
            "vat_lacznie": round(
                result.total - (energy_net_with_excise + distribution_net), 4
            ),
            "cena_laczna_netto": round(energy_net_with_excise + distribution_net, 4),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose an auditable split of the current price."""

        result = self._breakdown()
        settings = self._settings()
        custom = settings.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_CUSTOM
        system_rates = self.coordinator.data.system_net or {}
        network_net = float(
            (self.coordinator.data.distribution_net or self._tariff.distribution_net)[
                result.zone_key
            ]
        )
        quality_net = float(system_rates.get("quality", 0))
        oze_net = float(system_rates.get("oze", 0))
        cogeneration_net = float(system_rates.get("cogeneration", 0))
        system_net = quality_net + oze_net + cogeneration_net
        distribution_net = network_net + system_net
        energy_net_with_excise = result.energy / VAT
        energy_net_without_excise = max(
            0.0, energy_net_with_excise - EXCISE_NET_PLN_KWH
        )
        energy_vat = result.energy - energy_net_with_excise
        distribution_vat = result.distribution - distribution_net
        total_net = energy_net_with_excise + distribution_net
        total_vat = result.total - total_net

        source_names = {
            "ure": "aktualny arkusz URE",
            "ure_cache": "ostatni poprawny arkusz URE z pamięci podręcznej",
            "tauron_g13s": "aktualny oficjalny cennik G13s TAURON",
            "tauron_g13s_cache": (
                "ostatni poprawny cennik G13s TAURON z pamięci podręcznej"
            ),
            "bundled": "zweryfikowane stawki wbudowane",
        }
        attributes: dict[str, Any] = {
            "Cena łączna brutto [PLN/kWh]": round(result.total, 4),
            "Cena łączna netto [PLN/kWh]": round(total_net, 4),
            "VAT łącznie [PLN/kWh]": round(total_vat, 4),
            "Energia czynna brutto [PLN/kWh]": round(result.energy, 4),
            "Energia czynna netto z akcyzą [PLN/kWh]": round(energy_net_with_excise, 4),
            "Energia czynna netto bez akcyzy [PLN/kWh]": round(
                energy_net_without_excise, 4
            ),
            "Akcyza netto [PLN/kWh]": EXCISE_NET_PLN_KWH,
            "VAT od energii i akcyzy [PLN/kWh]": round(energy_vat, 4),
            "Dystrybucja brutto [PLN/kWh]": round(result.distribution, 4),
            "Dystrybucja netto [PLN/kWh]": round(distribution_net, 4),
            "Sieć zmienna netto [PLN/kWh]": round(network_net, 4),
            "Opłata jakościowa netto [PLN/kWh]": round(quality_net, 4),
            "Opłata OZE netto [PLN/kWh]": round(oze_net, 4),
            "Opłata kogeneracyjna netto [PLN/kWh]": round(cogeneration_net, 4),
            "VAT od dystrybucji [PLN/kWh]": round(distribution_vat, 4),
            "Operator sieci": OPERATOR_NAMES[self._operator],
            "Grupa taryfowa": self._group,
            "Aktywna strefa": result.zone_name,
            "Klucz strefy": result.zone_key,
            "Źródło ceny energii": (
                "własna umowa użytkownika"
                if custom
                else source_names.get(
                    self.coordinator.data.source, self.coordinator.data.source
                )
            ),
            "Sprzedawca energii": (
                "własna umowa użytkownika" if custom else SELLER_NAMES[self._operator]
            ),
            "Opłaty stałe uwzględnione": "Nie",
            "Obowiązuje od": self.coordinator.data.valid_from,
            "Obowiązuje do": self.coordinator.data.valid_until,
            "Źródło taryfy dystrybucyjnej": (
                self.coordinator.data.distribution_source_url
            ),
            "Źródło opłaty jakościowej": self.coordinator.data.system_source_url,
            "Źródło opłaty OZE": self.coordinator.data.oze_source_url,
            "Źródło opłaty kogeneracyjnej": (
                self.coordinator.data.cogeneration_source_url
            ),
            "Ostatnia kontrola taryf urzędowych": (
                self.coordinator.data.official_last_checked
            ),
            "Ostatnia aktualizacja taryf urzędowych": (
                self.coordinator.data.official_last_updated
            ),
            "Ostatni błąd taryf urzędowych": (
                self.coordinator.data.official_error or "Brak"
            ),
        }
        if not custom:
            attributes.update(
                {
                    "Adres źródła ceny energii": self.coordinator.data.source_url,
                    "Ostatnia kontrola ceny energii": self.coordinator.data.last_checked,
                    "Ostatnia aktualizacja ceny energii": (
                        self.coordinator.data.last_updated
                    ),
                    "Ostatni błąd ceny energii": self.coordinator.data.error or "Brak",
                }
            )
        return attributes


class PolishEnergyPriceComponentSensor(
    CoordinatorEntity[EnergyPriceCoordinator], SensorEntity
):
    """One native diagnostic entity used to present the price breakdown."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "PLN/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: EnergyPriceCoordinator,
        price_sensor: PolishEnergyPriceSensor,
        component_key: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._price_sensor = price_sensor
        self._component_key = component_key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_price_component_{component_key}"
        self._attr_suggested_object_id = component_key
        self._attr_device_info = price_sensor.device_info

    @property
    def available(self) -> bool:
        """Follow the validity of the complete annual price bundle."""

        return super().available and self.coordinator.data.is_valid_on(
            dt_util.now().date()
        )

    @property
    def native_value(self) -> float | None:
        """Return this component in PLN/kWh."""

        if not self.available:
            return None
        return self._price_sensor.price_components()[self._component_key]
