"""Home Assistant adapter for the platform-independent price source engine."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from aiohttp import ClientTimeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_OPERATOR,
    CONF_PRICE_SOURCE,
    CONF_TARIFF,
    DOMAIN,
    PRICE_SOURCE_REGULATED,
)
from .source_engine import (
    EnergyPriceData,
    EnergyPriceSourceEngine,
)

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=12)
DYNAMIC_UPDATE_INTERVAL = timedelta(hours=1)
REQUEST_TIMEOUT = ClientTimeout(total=45)
STORAGE_VERSION = 1
REQUEST_HEADERS = {"User-Agent": "Home Assistant PolishEnergyPrices/1.6.1"}


class EnergyPriceCoordinator(DataUpdateCoordinator[EnergyPriceData]):
    """Store and schedule data returned by the shared source engine."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.operator = str(entry.data[CONF_OPERATOR])
        self.group = str(entry.data[CONF_TARIFF])
        settings = {**entry.data, **entry.options}
        source = str(settings.get(CONF_PRICE_SOURCE, PRICE_SOURCE_REGULATED))
        self.engine = EnergyPriceSourceEngine(
            self.operator, self.group, source, _LOGGER
        )
        self.tariff = self.engine.tariff
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            config_entry=entry,
            update_interval=(
                DYNAMIC_UPDATE_INTERVAL
                if self.tariff.dynamic_zone_source
                else UPDATE_INTERVAL
            ),
        )
        self.data = self.engine.initial_data()

    async def async_initialize(self) -> None:
        """Load the last valid result before contacting official sources."""

        stored = await self.store.async_load()
        if stored is not None:
            try:
                self.data = self.engine.data_from_cache(stored)
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid cached prices for %s:%s",
                    self.operator,
                    self.group,
                )
        await self.async_config_entry_first_refresh()

    async def _get_bytes(
        self, url: str, limit: int, *, referer: str | None = None
    ) -> bytes:
        session = async_get_clientsession(self.hass)
        headers = dict(REQUEST_HEADERS)
        if referer:
            headers["Referer"] = referer
        async with session.get(
            url, timeout=REQUEST_TIMEOUT, headers=headers
        ) as response:
            response.raise_for_status()
            declared = response.content_length
            if declared is not None and declared > limit:
                raise ValueError(f"Odpowiedź źródła przekracza limit {limit} bajtów")
            content = await response.read()
        if len(content) > limit:
            raise ValueError(f"Odpowiedź źródła przekracza limit {limit} bajtów")
        return content

    async def _async_update_data(self) -> EnergyPriceData:
        current = await self.engine.refresh(
            self.data,
            self._get_bytes,
            self.hass.async_add_executor_job,
            dt_util.now(),
        )
        await self.store.async_save(self.engine.cache_payload(current))
        return current
