"""Constants for the Polish Energy Prices integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "polish_energy_price"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONF_OPERATOR = "operator"
CONF_TARIFF = "tariff"
CONF_PRICE_SOURCE = "price_source"
CONF_CUSTOM_PRICES = "custom_prices"
CONF_DAY_HOURS = "day_hours"
CONF_METER_CLOCK = "meter_clock"
CONF_EXTERNAL_STATISTICS = "external_statistics"
CONF_EXTERNAL_STATISTIC_PREFIX = "external_statistic_"
CONF_EXPORT_SETTLEMENT = "export_settlement"
CONF_EXPORT_CORRECTION = "export_correction"

PRICE_SOURCE_REGULATED = "regulated"
PRICE_SOURCE_CUSTOM = "custom"
PRICE_SOURCE_TAURON_G13S = "tauron_g13s"
PRICE_SOURCE_TAURON_G14DYNAMIC = "tauron_g14dynamic"

METER_CLOCK_LOCAL = "local_time"
METER_CLOCK_FIXED_WINTER = "fixed_winter_time"

EXPORT_SETTLEMENT_OFF = "off"
EXPORT_SETTLEMENT_RCE = "rce"
EXPORT_SETTLEMENT_RCEM = "rcem"

DEFAULT_EXPORT_CORRECTION = 1.0


def external_statistic_key(zone: str) -> str:
    """Return the config key used to map a tariff zone to an energy statistic."""

    return f"{CONF_EXTERNAL_STATISTIC_PREFIX}{zone}"
