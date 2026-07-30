"""Periodic refresh of official energy and distribution prices."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
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
    PRICE_SOURCE_REGULATED,
    PRICE_SOURCE_TAURON_G13S,
)
from .tariff import get_tariff
from .official import (
    OZE_RATES_PAGE,
    OPERATOR_PAGES,
    TAURON_G13S_PAGE,
    cogeneration_document_from_eli,
    discover_distribution_document,
    discover_quality_document,
    discover_tauron_g13s_script,
    eli_search_url,
    oze_rate_from_page,
    parse_cogeneration_rate_pdf,
    parse_distribution_pdf,
    parse_quality_rate_pdf,
    parse_tauron_g13s_prices,
)
from .ure import URE_OFFERS_PAGE, discover_workbook_url, parse_ure_workbook

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=12)
REQUEST_TIMEOUT = ClientTimeout(total=45)
MAX_PAGE_BYTES = 2_000_000
MAX_WORKBOOK_BYTES = 5_000_000
MAX_PDF_BYTES = 15_000_000
STORAGE_VERSION = 1
REQUEST_HEADERS = {"User-Agent": "Home Assistant PolishEnergyPrices/1.3.0"}


@dataclass(slots=True)
class EnergyPriceData:
    """Current prices plus provenance shown by the sensor."""

    prices: dict[str, float]
    source: str
    source_url: str | None = None
    last_checked: str | None = None
    last_updated: str | None = None
    error: str | None = None
    distribution_net: dict[str, float] | None = None
    system_net: dict[str, float] | None = None
    valid_from: str = "2026-01-01"
    valid_until: str = "2026-12-31"
    distribution_source_url: str | None = None
    system_source_url: str | None = None
    oze_source_url: str | None = None
    cogeneration_source_url: str | None = None
    official_last_checked: str | None = None
    official_last_updated: str | None = None
    official_error: str | None = None

    @property
    def system_total(self) -> float:
        """Return all common net variable system charges."""

        rates = self.system_net or {
            "quality": 0.0332,
            "oze": 0.0073,
            "cogeneration": 0.0030,
        }
        return round(sum(float(value) for value in rates.values()), 6)

    def is_valid_on(self, day: date) -> bool:
        """Return whether the active official annual bundle covers ``day``."""

        return (
            date.fromisoformat(self.valid_from)
            <= day
            <= date.fromisoformat(self.valid_until)
        )


class EnergyPriceCoordinator(DataUpdateCoordinator[EnergyPriceData]):
    """Check official sources and cache the latest complete valid result."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.operator = str(entry.data[CONF_OPERATOR])
        self.group = str(entry.data[CONF_TARIFF])
        self.tariff = get_tariff(self.operator, self.group)
        settings = {**entry.data, **entry.options}
        source = settings.get(CONF_PRICE_SOURCE, PRICE_SOURCE_REGULATED)
        self.use_ure = source == PRICE_SOURCE_REGULATED
        self.use_tauron_g13s = source == PRICE_SOURCE_TAURON_G13S
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            config_entry=entry,
            update_interval=UPDATE_INTERVAL,
        )
        self.data = EnergyPriceData(
            dict(self.tariff.energy_gross),
            "bundled",
            distribution_net=dict(self.tariff.distribution_net),
            system_net={
                "quality": 0.0332,
                "oze": 0.0073,
                "cogeneration": 0.0030,
            },
        )

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
                    distribution = self._validated_distribution(
                        stored.get("distribution_net", self.tariff.distribution_net)
                    )
                    system = self._validated_system(
                        stored.get(
                            "system_net",
                            {"quality": 0.0332, "oze": 0.0073, "cogeneration": 0.0030},
                        )
                    )
                    stored_source = str(stored.get("source", "bundled"))
                    if stored.get("source_url"):
                        if stored_source.startswith("tauron_g13s"):
                            stored_source = "tauron_g13s_cache"
                        elif stored_source.startswith("ure"):
                            stored_source = "ure_cache"
                    self.data = EnergyPriceData(
                        prices=prices,
                        source=stored_source,
                        source_url=stored.get("source_url"),
                        last_checked=stored.get("last_checked"),
                        last_updated=stored.get("last_updated"),
                        error=stored.get("error"),
                        distribution_net=distribution,
                        system_net=system,
                        valid_from=str(stored.get("valid_from", "2026-01-01")),
                        valid_until=str(stored.get("valid_until", "2026-12-31")),
                        distribution_source_url=stored.get("distribution_source_url"),
                        system_source_url=stored.get("system_source_url"),
                        oze_source_url=stored.get("oze_source_url"),
                        cogeneration_source_url=stored.get("cogeneration_source_url"),
                        official_last_checked=stored.get("official_last_checked"),
                        official_last_updated=stored.get("official_last_updated"),
                        official_error=stored.get("official_error"),
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

    def _validated_distribution(self, raw: object) -> dict[str, float]:
        if not isinstance(raw, dict) or set(raw) != set(self.tariff.zones):
            raise ValueError("Niepełny zestaw stawek dystrybucyjnych")
        rates = {zone: round(float(raw[zone]), 6) for zone in self.tariff.zones}
        if any(not 0.001 <= rate <= 1.5 for rate in rates.values()):
            raise ValueError("Stawka dystrybucyjna poza bezpiecznym zakresem")
        return rates

    @staticmethod
    def _validated_system(raw: object) -> dict[str, float]:
        keys = {"quality", "oze", "cogeneration"}
        if not isinstance(raw, dict) or set(raw) != keys:
            raise ValueError("Niepełny zestaw opłat systemowych")
        rates = {key: round(float(raw[key]), 6) for key in keys}
        if not 0.005 <= sum(rates.values()) <= 0.2 or any(
            rate < 0 for rate in rates.values()
        ):
            raise ValueError("Opłaty systemowe poza bezpiecznym zakresem")
        return rates

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

    async def _refresh_energy(
        self, current: EnergyPriceData, checked: str
    ) -> EnergyPriceData:
        if self.use_tauron_g13s:
            return await self._refresh_tauron_g13s(current, checked)
        if not self.use_ure:
            return replace(current, error=None)
        try:
            page = (await self._get_bytes(URE_OFFERS_PAGE, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            workbook_url = discover_workbook_url(page)

            if current.source_url == workbook_url and current.source != "bundled":
                current = replace(
                    current, source="ure", last_checked=checked, error=None
                )
            else:
                workbook = await self._get_bytes(
                    workbook_url, MAX_WORKBOOK_BYTES, referer=URE_OFFERS_PAGE
                )
                parsed = await self.hass.async_add_executor_job(
                    parse_ure_workbook, workbook, self.operator, self.group
                )
                current = replace(
                    current,
                    prices=self._validated_prices(parsed),
                    source="ure",
                    source_url=workbook_url,
                    last_checked=checked,
                    last_updated=checked,
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
                current,
                source="ure_cache" if current.source_url else "bundled",
                last_checked=checked,
                error=str(err),
            )

    async def _refresh_tauron_g13s(
        self, current: EnergyPriceData, checked: str
    ) -> EnergyPriceData:
        """Refresh the current offer published on the official TAURON page."""

        try:
            page = (await self._get_bytes(TAURON_G13S_PAGE, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            script_url = discover_tauron_g13s_script(page)
            script = (await self._get_bytes(script_url, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            parsed = self._validated_prices(parse_tauron_g13s_prices(script))
            changed = current.prices != parsed or current.source_url != script_url
            return replace(
                current,
                prices=parsed,
                source="tauron_g13s",
                source_url=script_url,
                last_checked=checked,
                last_updated=checked if changed else current.last_updated,
                error=None,
            )
        except (ClientError, TimeoutError, UnicodeError, ValueError) as err:
            _LOGGER.warning("G13s energy price refresh failed: %s", err)
            return replace(
                current,
                source="tauron_g13s_cache" if current.source_url else "bundled",
                last_checked=checked,
                error=str(err),
            )

    async def _refresh_official(
        self, current: EnergyPriceData, checked: str
    ) -> EnergyPriceData:
        year = dt_util.now().year
        if year < 2026:
            return current
        try:
            operator_page_url = OPERATOR_PAGES[self.operator]
            operator_page = (
                await self._get_bytes(operator_page_url, MAX_PAGE_BYTES)
            ).decode("utf-8", errors="replace")
            distribution_document = discover_distribution_document(
                operator_page, operator_page_url, self.operator, year
            )
            quality_document = discover_quality_document(
                operator_page,
                operator_page_url,
                self.operator,
                year,
                distribution_document,
            )

            distribution = current.distribution_net
            system = current.system_net
            changed = False
            warning: str | None = None
            quality_source_url = quality_document.url
            # Operators occasionally replace a file without changing its URL,
            # therefore values are parsed on every scheduled check.
            distribution_content = await self._get_bytes(
                distribution_document.url,
                MAX_PDF_BYTES,
                referer=operator_page_url,
            )
            parsed = await self.hass.async_add_executor_job(
                parse_distribution_pdf,
                distribution_content,
                self.operator,
                self.group,
                self.tariff.zones,
                year,
            )
            parsed_distribution = self._validated_distribution(parsed)
            changed = changed or distribution != parsed_distribution
            changed = changed or (
                current.distribution_source_url != distribution_document.url
            )
            distribution = parsed_distribution

            quality_content = distribution_content
            if quality_document.url != distribution_document.url:
                quality_content = await self._get_bytes(
                    quality_document.url,
                    MAX_PDF_BYTES,
                    referer=operator_page_url,
                )
            quality = await self.hass.async_add_executor_job(
                parse_quality_rate_pdf, quality_content, year
            )
            if (
                self.operator == "pge"
                and year == 2026
                and dt_util.now().date() >= date(2026, 2, 1)
                and quality != 0.0332
            ):
                # The official PGE host currently fails certificate-chain
                # validation, while its official mirror still exposes the
                # January document. Retain the audited last-known-good
                # quality rate, but continue updating the other sources.
                quality = float((system or {})["quality"])
                quality_source_url = current.system_source_url
                warning = (
                    "PGE: dokument pośredni ma stawkę sprzed 1.02.2026; "
                    "zachowano ostatnią zweryfikowaną stawkę jakościową"
                )
            quality_changed = (
                float((system or {}).get("quality", -1)) != quality
                or current.system_source_url != quality_source_url
            )
            system = dict(system or {})
            system["quality"] = quality
            changed = changed or quality_changed

            oze_page = (await self._get_bytes(OZE_RATES_PAGE, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            oze = oze_rate_from_page(oze_page, year)
            if system is None or system.get("oze") != oze:
                system = dict(system or {})
                system["oze"] = oze
                changed = True

            search_url = eli_search_url(year)
            eli_result = await self._get_bytes(search_url, MAX_PAGE_BYTES)
            cogeneration_document = cogeneration_document_from_eli(eli_result, year)
            if current.cogeneration_source_url != cogeneration_document.url:
                cogeneration_pdf = await self._get_bytes(
                    cogeneration_document.url,
                    MAX_PDF_BYTES,
                    referer=search_url,
                )
                cogeneration = await self.hass.async_add_executor_job(
                    parse_cogeneration_rate_pdf, cogeneration_pdf, year
                )
                system = dict(system or {})
                system["cogeneration"] = cogeneration
                changed = True
            # Both parts must be complete before either one is activated.
            if distribution is None or system is None:
                raise ValueError("Nie udało się zbudować kompletnego zestawu taryf")
            system = self._validated_system(system)
            return replace(
                current,
                distribution_net=distribution,
                system_net=system,
                valid_from=f"{year}-01-01",
                valid_until=f"{year}-12-31",
                distribution_source_url=distribution_document.url,
                system_source_url=quality_source_url,
                oze_source_url=OZE_RATES_PAGE,
                cogeneration_source_url=cogeneration_document.url,
                official_last_checked=checked,
                official_last_updated=(
                    checked if changed else current.official_last_updated
                ),
                official_error=warning,
            )
        except (ClientError, TimeoutError, UnicodeError, ValueError) as err:
            _LOGGER.warning(
                "Official tariff refresh failed for %s:%s: %s",
                self.operator,
                self.group,
                err,
            )
            return replace(
                current, official_last_checked=checked, official_error=str(err)
            )

    async def _async_update_data(self) -> EnergyPriceData:
        checked = dt_util.now().isoformat()
        current = await self._refresh_energy(self.data, checked)
        current = await self._refresh_official(current, checked)
        await self.store.async_save(
            {
                "operator": self.operator,
                "group": self.group,
                **asdict(current),
            }
        )
        return current
