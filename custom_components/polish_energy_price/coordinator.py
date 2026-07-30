"""Periodic refresh of regulated energy prices from URE."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import logging
from typing import Any

from aiohttp import ClientError, ClientTimeout

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
    PRICE_SOURCE_CUSTOM,
)
from .tariff import get_tariff
from .ure import URE_OFFERS_PAGE, discover_workbook_url, parse_ure_workbook

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=12)
REQUEST_TIMEOUT = ClientTimeout(total=45)
MAX_PAGE_BYTES = 2_000_000
MAX_WORKBOOK_BYTES = 5_000_000
STORAGE_VERSION = 1
REQUEST_HEADERS = {"User-Agent": "Home Assistant PolishEnergyPrices/1.2.1"}


@dataclass(slots=True)
class EnergyPriceData:
    """Current prices plus provenance shown by the sensor."""

    prices: dict[str, float]
    source: str
    source_url: str | None = None
    last_checked: str | None = None
    last_updated: str | None = None
    error: str | None = None


class EnergyPriceCoordinator(DataUpdateCoordinator[EnergyPriceData]):
    """Check the stable URE page and cache the latest valid workbook result."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.operator = str(entry.data[CONF_OPERATOR])
        self.group = str(entry.data[CONF_TARIFF])
        self.tariff = get_tariff(self.operator, self.group)
        settings = {**entry.data, **entry.options}
        self.use_ure = settings.get(CONF_PRICE_SOURCE) != PRICE_SOURCE_CUSTOM
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            config_entry=entry,
            update_interval=UPDATE_INTERVAL if self.use_ure else None,
        )
        self.data = EnergyPriceData(dict(self.tariff.energy_gross), "bundled")

    async def async_initialize(self) -> None:
        """Load a last-known-good result before contacting URE."""

        stored = await self.store.async_load()
        if isinstance(stored, dict):
            try:
                if (
                    stored.get("operator") == self.operator
                    and stored.get("group") == self.group
                ):
                    prices = self._validated_prices(stored["prices"])
                    self.data = EnergyPriceData(
                        prices=prices,
                        source="ure_cache",
                        source_url=stored.get("source_url"),
                        last_checked=stored.get("last_checked"),
                        last_updated=stored.get("last_updated"),
                    )
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid cached URE prices for %s:%s",
                    self.operator,
                    self.group,
                )
        await self.async_config_entry_first_refresh()

    def _validated_prices(self, raw: object) -> dict[str, float]:
        if not isinstance(raw, dict) or set(raw) != set(self.tariff.zones):
            raise ValueError("Niepełny zestaw stref")
        prices = {zone: round(float(raw[zone]), 4) for zone in self.tariff.zones}
        if any(not 0 < price < 10 for price in prices.values()):
            raise ValueError("Cena poza bezpiecznym zakresem")
        return prices

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
                raise ValueError(f"Odpowiedź URE przekracza limit {limit} bajtów")
            content = await response.read()
        if len(content) > limit:
            raise ValueError(f"Odpowiedź URE przekracza limit {limit} bajtów")
        return content

    async def _async_update_data(self) -> EnergyPriceData:
        if not self.use_ure:
            return self.data
        checked = dt_util.now().isoformat()
        try:
            page = (await self._get_bytes(URE_OFFERS_PAGE, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            workbook_url = discover_workbook_url(page)

            if self.data.source_url == workbook_url and self.data.source != "bundled":
                current = replace(
                    self.data, source="ure", last_checked=checked, error=None
                )
            else:
                workbook = await self._get_bytes(
                    workbook_url, MAX_WORKBOOK_BYTES, referer=URE_OFFERS_PAGE
                )
                parsed = await self.hass.async_add_executor_job(
                    parse_ure_workbook, workbook, self.operator, self.group
                )
                current = EnergyPriceData(
                    prices=self._validated_prices(parsed),
                    source="ure",
                    source_url=workbook_url,
                    last_checked=checked,
                    last_updated=checked,
                )
            await self.store.async_save(
                {
                    "operator": self.operator,
                    "group": self.group,
                    **asdict(current),
                }
            )
            return current
        except (ClientError, TimeoutError, UnicodeError, ValueError) as err:
            # Availability is more important than a transient remote failure.
            # Retain the cache, or the audited bundled price on the first run.
            _LOGGER.warning(
                "URE price refresh failed for %s:%s: %s",
                self.operator,
                self.group,
                err,
            )
            return replace(
                self.data,
                source="ure_cache" if self.data.source_url else "bundled",
                last_checked=checked,
                error=str(err),
            )
