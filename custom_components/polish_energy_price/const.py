"""Constants for the Polish Energy Prices integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "polish_energy_price"
PLATFORMS: list[Platform] = [Platform.SENSOR]

CONF_OPERATOR = "operator"
CONF_TARIFF = "tariff"
CONF_PRICE_SOURCE = "price_source"
CONF_CUSTOM_PRICES = "custom_prices"
CONF_DAY_HOURS = "day_hours"
CONF_METER_CLOCK = "meter_clock"
CONF_EXTERNAL_STATISTICS = "external_statistics"
CONF_EXTERNAL_STATISTIC_PREFIX = "external_statistic_"

PRICE_SOURCE_REGULATED = "regulated"
PRICE_SOURCE_CUSTOM = "custom"
PRICE_SOURCE_TAURON_G13S = "tauron_g13s"

METER_CLOCK_LOCAL = "local_time"
METER_CLOCK_FIXED_WINTER = "fixed_winter_time"


def external_statistic_key(zone: str) -> str:
    """Return the config key used to map a tariff zone to an energy statistic."""

    return f"{CONF_EXTERNAL_STATISTIC_PREFIX}{zone}"
