"""Platform-independent refresh engine for official Polish energy prices."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
import logging
from typing import Any

from .official import (
    OZE_RATES_PAGE,
    OPERATOR_PAGES,
    TAURON_G13S_PAGE,
    TAURON_G14DYNAMIC_PRICE_LIST,
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
    parse_tauron_g14dynamic_prices,
)
from .kompas import (
    KOMPAS_API_URL,
    build_kompas_url,
    local_day_for_key,
    parse_kompas,
    validate_dynamic_zones,
)
from .tariff import WARSAW, TariffDefinition, get_tariff
from .ure import URE_OFFERS_PAGE, discover_workbook_url, parse_ure_workbook

MAX_PAGE_BYTES = 2_000_000
MAX_WORKBOOK_BYTES = 5_000_000
MAX_PDF_BYTES = 15_000_000
MAX_KOMPAS_BYTES = 1_000_000

FetchBytes = Callable[..., Awaitable[bytes]]
RunSync = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class EnergyPriceData:
    """Current prices and the official sources used to obtain them."""

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
    dynamic_zones: dict[str, str] | None = None
    dynamic_source_url: str | None = None
    dynamic_last_checked: str | None = None
    dynamic_last_updated: str | None = None
    dynamic_publication_utc: str | None = None
    dynamic_error: str | None = None

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


class EnergyPriceSourceEngine:
    """Refresh and validate prices without depending on an automation platform."""

    def __init__(
        self,
        operator: str,
        group: str,
        price_source: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self.operator = operator
        self.group = group
        self.tariff: TariffDefinition = get_tariff(operator, group)
        self.use_ure = price_source == "regulated"
        self.use_tauron_g13s = price_source == "tauron_g13s"
        self.use_tauron_g14dynamic = price_source == "tauron_g14dynamic"
        self.logger = logger or logging.getLogger(__name__)

    def initial_data(self) -> EnergyPriceData:
        """Return the audited bundled prices used before the first refresh."""

        return EnergyPriceData(
            dict(self.tariff.energy_gross),
            "bundled",
            distribution_net=dict(self.tariff.distribution_net),
            system_net={
                "quality": 0.0332,
                "oze": 0.0073,
                "cogeneration": 0.0030,
            },
            dynamic_zones={} if self.tariff.dynamic_zone_source else None,
        )

    def data_from_cache(self, stored: object) -> EnergyPriceData:
        """Validate and restore one last-known-good cache payload."""

        if not isinstance(stored, Mapping):
            raise ValueError("Pamięć podręczna nie jest obiektem")
        if stored.get("operator") != self.operator or stored.get("group") != self.group:
            raise ValueError("Pamięć podręczna dotyczy innej taryfy")
        prices = self.validate_prices(stored["prices"])
        distribution = self.validate_distribution(
            stored.get("distribution_net", self.tariff.distribution_net)
        )
        system = self.validate_system(
            stored.get(
                "system_net",
                {"quality": 0.0332, "oze": 0.0073, "cogeneration": 0.0030},
            )
        )
        stored_source = str(stored.get("source", "bundled"))
        if stored.get("source_url"):
            if stored_source.startswith("tauron_g13s"):
                stored_source = "tauron_g13s_cache"
            elif stored_source.startswith("tauron_g14dynamic"):
                stored_source = "tauron_g14dynamic_cache"
            elif stored_source.startswith("ure"):
                stored_source = "ure_cache"
        return EnergyPriceData(
            prices=prices,
            source=stored_source,
            source_url=_optional_str(stored.get("source_url")),
            last_checked=_optional_str(stored.get("last_checked")),
            last_updated=_optional_str(stored.get("last_updated")),
            error=_optional_str(stored.get("error")),
            distribution_net=distribution,
            system_net=system,
            valid_from=str(stored.get("valid_from", "2026-01-01")),
            valid_until=str(stored.get("valid_until", "2026-12-31")),
            distribution_source_url=_optional_str(
                stored.get("distribution_source_url")
            ),
            system_source_url=_optional_str(stored.get("system_source_url")),
            oze_source_url=_optional_str(stored.get("oze_source_url")),
            cogeneration_source_url=_optional_str(
                stored.get("cogeneration_source_url")
            ),
            official_last_checked=_optional_str(
                stored.get("official_last_checked")
            ),
            official_last_updated=_optional_str(
                stored.get("official_last_updated")
            ),
            official_error=_optional_str(stored.get("official_error")),
            dynamic_zones=(
                validate_dynamic_zones(stored.get("dynamic_zones"))
                if self.tariff.dynamic_zone_source
                else None
            ),
            dynamic_source_url=_optional_str(stored.get("dynamic_source_url")),
            dynamic_last_checked=_optional_str(
                stored.get("dynamic_last_checked")
            ),
            dynamic_last_updated=_optional_str(
                stored.get("dynamic_last_updated")
            ),
            dynamic_publication_utc=_optional_str(
                stored.get("dynamic_publication_utc")
            ),
            dynamic_error=_optional_str(stored.get("dynamic_error")),
        )

    def cache_payload(self, data: EnergyPriceData) -> dict[str, Any]:
        """Build a JSON-serializable cache payload."""

        return {
            "operator": self.operator,
            "group": self.group,
            **asdict(data),
        }

    def validate_prices(self, raw: object) -> dict[str, float]:
        """Validate a complete gross energy-price mapping."""

        if not isinstance(raw, Mapping) or set(raw) != set(self.tariff.zones):
            raise ValueError("Niepełny zestaw stref")
        prices = {zone: round(float(raw[zone]), 4) for zone in self.tariff.zones}
        if any(not 0 < price < 10 for price in prices.values()):
            raise ValueError("Cena poza bezpiecznym zakresem")
        return prices

    def validate_distribution(self, raw: object) -> dict[str, float]:
        """Validate all variable network rates for the tariff."""

        if not isinstance(raw, Mapping) or set(raw) != set(self.tariff.zones):
            raise ValueError("Niepełny zestaw stawek dystrybucyjnych")
        rates = {zone: round(float(raw[zone]), 6) for zone in self.tariff.zones}
        if any(not 0.001 <= rate <= 5 for rate in rates.values()):
            raise ValueError("Stawka dystrybucyjna poza bezpiecznym zakresem")
        return rates

    @staticmethod
    def validate_system(raw: object) -> dict[str, float]:
        """Validate the quality, OZE and cogeneration rates."""

        keys = {"quality", "oze", "cogeneration"}
        if not isinstance(raw, Mapping) or set(raw) != keys:
            raise ValueError("Niepełny zestaw opłat systemowych")
        rates = {key: round(float(raw[key]), 6) for key in keys}
        if not 0.005 <= sum(rates.values()) <= 0.2 or any(
            rate < 0 for rate in rates.values()
        ):
            raise ValueError("Opłaty systemowe poza bezpiecznym zakresem")
        return rates

    async def refresh(
        self,
        current: EnergyPriceData,
        fetch_bytes: FetchBytes,
        run_sync: RunSync,
        now: datetime,
    ) -> EnergyPriceData:
        """Refresh energy and distribution prices from official sources."""

        checked = now.isoformat()
        current = await self._refresh_energy(
            current, checked, fetch_bytes, run_sync
        )
        if not self._recent_official_check(current, now):
            current = await self._refresh_official(
                current, checked, fetch_bytes, run_sync, now
            )
        return await self._refresh_dynamic(current, checked, fetch_bytes, now)

    async def _refresh_energy(
        self,
        current: EnergyPriceData,
        checked: str,
        fetch_bytes: FetchBytes,
        run_sync: RunSync,
    ) -> EnergyPriceData:
        if self.use_tauron_g13s:
            return await self._refresh_tauron_g13s(current, checked, fetch_bytes)
        if self.use_tauron_g14dynamic:
            if (
                current.source_url
                and not current.error
                and self._checked_within(
                    current.last_checked,
                    datetime.fromisoformat(checked),
                    timedelta(hours=12),
                )
            ):
                return current
            return await self._refresh_tauron_g14dynamic(
                current, checked, fetch_bytes, run_sync
            )
        if not self.use_ure:
            return replace(current, error=None)
        try:
            page = (await fetch_bytes(URE_OFFERS_PAGE, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            workbook_url = discover_workbook_url(page)
            if current.source_url == workbook_url and current.source != "bundled":
                return replace(
                    current, source="ure", last_checked=checked, error=None
                )
            workbook = await fetch_bytes(
                workbook_url, MAX_WORKBOOK_BYTES, referer=URE_OFFERS_PAGE
            )
            parsed = await run_sync(
                parse_ure_workbook, workbook, self.operator, self.group
            )
            return replace(
                current,
                prices=self.validate_prices(parsed),
                source="ure",
                source_url=workbook_url,
                last_checked=checked,
                last_updated=checked,
                error=None,
            )
        except Exception as err:  # noqa: BLE001 - retain the last valid bundle
            self.logger.warning(
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
        self,
        current: EnergyPriceData,
        checked: str,
        fetch_bytes: FetchBytes,
    ) -> EnergyPriceData:
        try:
            page = (await fetch_bytes(TAURON_G13S_PAGE, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            script_url = discover_tauron_g13s_script(page)
            script = (await fetch_bytes(script_url, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            parsed = self.validate_prices(parse_tauron_g13s_prices(script))
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
        except Exception as err:  # noqa: BLE001 - retain the last valid bundle
            self.logger.warning("G13s energy price refresh failed: %s", err)
            return replace(
                current,
                source="tauron_g13s_cache" if current.source_url else "bundled",
                last_checked=checked,
                error=str(err),
            )

    async def _refresh_tauron_g14dynamic(
        self,
        current: EnergyPriceData,
        checked: str,
        fetch_bytes: FetchBytes,
        run_sync: RunSync,
    ) -> EnergyPriceData:
        try:
            content = await fetch_bytes(
                TAURON_G14DYNAMIC_PRICE_LIST, MAX_PDF_BYTES
            )
            parsed = self.validate_prices(
                await run_sync(parse_tauron_g14dynamic_prices, content)
            )
            changed = (
                current.prices != parsed
                or current.source_url != TAURON_G14DYNAMIC_PRICE_LIST
            )
            return replace(
                current,
                prices=parsed,
                source="tauron_g14dynamic",
                source_url=TAURON_G14DYNAMIC_PRICE_LIST,
                last_checked=checked,
                last_updated=checked if changed else current.last_updated,
                error=None,
            )
        except Exception as err:  # noqa: BLE001 - retain last valid prices
            self.logger.warning("G14dynamic energy price refresh failed: %s", err)
            return replace(
                current,
                source=(
                    "tauron_g14dynamic_cache"
                    if current.source_url
                    else "bundled"
                ),
                last_checked=checked,
                error=str(err),
            )

    def _recent_official_check(
        self, current: EnergyPriceData, now: datetime
    ) -> bool:
        """Avoid downloading annual tariff documents every Kompas refresh."""

        if not self.tariff.dynamic_zone_source:
            return False
        return self._checked_within(
            current.official_last_checked, now, timedelta(hours=12)
        )

    @staticmethod
    def _checked_within(
        value: str | None, now: datetime, interval: timedelta
    ) -> bool:
        if not value:
            return False
        try:
            last = datetime.fromisoformat(value)
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=now.tzinfo)
        elapsed = now - last
        return timedelta(0) <= elapsed < interval

    async def _refresh_dynamic(
        self,
        current: EnergyPriceData,
        checked: str,
        fetch_bytes: FetchBytes,
        now: datetime,
    ) -> EnergyPriceData:
        """Refresh active PSE revisions for today and the next two days."""

        if not self.tariff.dynamic_zone_source:
            return current

        local_today = now.astimezone(WARSAW).date()
        zones = dict(current.dynamic_zones or {})
        keep_from = local_today - timedelta(days=1)
        keep_until = local_today + timedelta(days=2)
        zones = {
            key: zone
            for key, zone in zones.items()
            if keep_from <= local_day_for_key(key) <= keep_until
        }
        newest_publication = current.dynamic_publication_utc
        changed = False
        errors: list[str] = []
        for offset in range(3):
            day = local_today + timedelta(days=offset)
            url = build_kompas_url(day)
            try:
                snapshot = parse_kompas(
                    await fetch_bytes(url, MAX_KOMPAS_BYTES)
                )
            except Exception as err:  # noqa: BLE001 - preserve cached revision
                errors.append(f"{day.isoformat()}: {err}")
                continue
            if not snapshot.zones:
                continue
            day_keys = {
                key for key in zones if local_day_for_key(key) == day
            }
            replacement = set(snapshot.zones)
            if day_keys != replacement or any(
                zones.get(key) != zone for key, zone in snapshot.zones.items()
            ):
                changed = True
            for key in day_keys - replacement:
                zones.pop(key, None)
            zones.update(snapshot.zones)
            if snapshot.publication_utc and (
                newest_publication is None
                or snapshot.publication_utc > newest_publication
            ):
                newest_publication = snapshot.publication_utc

        current_day_has_data = any(
            local_day_for_key(key) == local_today for key in zones
        )
        if not current_day_has_data:
            errors.append(f"brak stref dla bieżącej doby {local_today.isoformat()}")
        return replace(
            current,
            dynamic_zones=zones,
            dynamic_source_url=KOMPAS_API_URL,
            dynamic_last_checked=checked,
            dynamic_last_updated=(
                checked if changed else current.dynamic_last_updated
            ),
            dynamic_publication_utc=newest_publication,
            dynamic_error="; ".join(errors) or None,
        )

    async def _refresh_official(
        self,
        current: EnergyPriceData,
        checked: str,
        fetch_bytes: FetchBytes,
        run_sync: RunSync,
        now: datetime,
    ) -> EnergyPriceData:
        year = now.year
        if year < 2026:
            return current
        try:
            operator_page_url = OPERATOR_PAGES[self.operator]
            operator_page = (
                await fetch_bytes(operator_page_url, MAX_PAGE_BYTES)
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
            distribution_content = await fetch_bytes(
                distribution_document.url,
                MAX_PDF_BYTES,
                referer=operator_page_url,
            )
            parsed = await run_sync(
                parse_distribution_pdf,
                distribution_content,
                self.operator,
                self.group,
                self.tariff.zones,
                year,
            )
            parsed_distribution = self.validate_distribution(parsed)
            changed = changed or distribution != parsed_distribution
            changed = changed or (
                current.distribution_source_url != distribution_document.url
            )
            distribution = parsed_distribution

            quality_content = distribution_content
            if quality_document.url != distribution_document.url:
                quality_content = await fetch_bytes(
                    quality_document.url,
                    MAX_PDF_BYTES,
                    referer=operator_page_url,
                )
            quality = await run_sync(parse_quality_rate_pdf, quality_content, year)
            if (
                self.operator == "pge"
                and year == 2026
                and now.date() >= date(2026, 2, 1)
                and quality != 0.0332
            ):
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

            oze_page = (await fetch_bytes(OZE_RATES_PAGE, MAX_PAGE_BYTES)).decode(
                "utf-8", errors="replace"
            )
            oze = oze_rate_from_page(oze_page, year)
            if system.get("oze") != oze:
                system["oze"] = oze
                changed = True

            search_url = eli_search_url(year)
            eli_result = await fetch_bytes(search_url, MAX_PAGE_BYTES)
            cogeneration_document = cogeneration_document_from_eli(eli_result, year)
            if current.cogeneration_source_url != cogeneration_document.url:
                cogeneration_pdf = await fetch_bytes(
                    cogeneration_document.url,
                    MAX_PDF_BYTES,
                    referer=search_url,
                )
                cogeneration = await run_sync(
                    parse_cogeneration_rate_pdf, cogeneration_pdf, year
                )
                system["cogeneration"] = cogeneration
                changed = True
            if distribution is None:
                raise ValueError("Nie udało się zbudować kompletnego zestawu taryf")
            system = self.validate_system(system)
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
        except Exception as err:  # noqa: BLE001 - retain the last valid bundle
            self.logger.warning(
                "Official tariff refresh failed for %s:%s: %s",
                self.operator,
                self.group,
                err,
            )
            return replace(
                current, official_last_checked=checked, official_error=str(err)
            )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
