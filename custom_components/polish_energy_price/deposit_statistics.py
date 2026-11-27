"""Estimated prosumer deposit balance, rebuilt hourly from recorder statistics."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import partial
import logging
from typing import Any

from aiohttp import ClientTimeout

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    CONF_CUSTOM_PRICES,
    CONF_DEPOSIT_BALANCE,
    CONF_DEPOSIT_MONTH,
    CONF_DEPOSIT_TRANCHES,
    CONF_EXPORT_SETTLEMENT,
    CONF_EXPORT_STATISTIC,
    CONF_EXPORT_STATISTICS,
    CONF_EXTERNAL_STATISTICS,
    CONF_OPERATOR,
    CONF_PRICE_SOURCE,
    CONF_TARIFF,
    DOMAIN,
    EXPORT_SETTLEMENT_RCEM,
    PRICE_SOURCE_CUSTOM,
    external_statistic_key,
)
from .coordinator import EnergyPriceCoordinator
from .deposit import (
    add_months,
    inflow,
    month_range,
    monthly_inflows,
    opening_from_invoice,
    opening_tranches,
    parse_tranches,
    simulate,
)
from .export_price import (
    RCEM_PAGE,
    parse_rcem,
    rcem_month_key,
    rcem_statistics_start,
    validate_rcem,
)
from .tariff import WARSAW, get_tariff, price_for_zone

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class DepositStatisticsManager:
    """Estimate the prosumer deposit balance and expose it to the sensor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: EnergyPriceCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.tariff = get_tariff(
            str(entry.data[CONF_OPERATOR]), str(entry.data[CONF_TARIFF])
        )
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.deposit_statistics"
        )
        self._lock = asyncio.Lock()
        # Set by a successful refresh: tranches, the current month's cost so
        # far and its inflow so far (None while no RCEm is known at all).
        self.result: dict[str, Any] | None = None

    def _settings(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def enabled(self) -> bool:
        """Whether every requirement of the estimate is configured."""

        settings = self._settings()
        return not (
            settings.get(CONF_EXPORT_SETTLEMENT) != EXPORT_SETTLEMENT_RCEM
            or not settings.get(CONF_EXPORT_STATISTICS, False)
            or not settings.get(CONF_EXPORT_STATISTIC)
            or not settings.get(CONF_EXTERNAL_STATISTICS, False)
            or self.tariff.group.lower() in ("g13s", "g14dynamic")
            or not all(
                settings.get(external_statistic_key(zone))
                for zone in self.tariff.zones
            )
            or not (
                str(settings.get(CONF_DEPOSIT_TRANCHES) or "").strip()
                or (
                    settings.get(CONF_DEPOSIT_BALANCE) is not None
                    and settings.get(CONF_DEPOSIT_MONTH)
                )
            )
        )

    async def async_refresh(self, _now: datetime | None = None) -> None:
        """Refresh the estimate without breaking price updates."""

        if self._lock.locked():
            return
        async with self._lock:
            issue_id = f"deposit_{self.entry.entry_id}"
            try:
                self.result = await self._async_refresh() if self.enabled else None
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.exception("Invalid deposit estimate configuration")
                self.result = None
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="deposit_failed",
                    translation_placeholders={
                        "entry": self.entry.title,
                        "reason": str(err),
                    },
                )
            except Exception:  # noqa: BLE001 - recorder failures must not break pricing
                _LOGGER.exception("Could not refresh the deposit estimate")

    async def _rcem_prices(self, first: str, last: str) -> dict[str, float]:
        """Known RCEm for ``first``..``last``, fetching what the coordinator dropped."""

        stored = await self.store.async_load() or {}
        fresh = self.coordinator.data.rcem_prices or {}
        prices = {**validate_rcem(stored.get("prices")), **fresh}
        if fresh:
            # The coordinator keeps only 14 months; older ones come from the
            # PSE archive page (once per refresh while one is still missing).
            missing = {
                month
                for month in month_range(first, last)
                if month < min(fresh) and month not in prices
            }
            if missing:
                prices.update(await self._fetch_rcem({m[:4] for m in missing}))
        needed = {
            month: prices[month]
            for month in month_range(first, last)
            if month in prices
        }
        if needed != stored.get("prices"):
            await self.store.async_save({"prices": needed})
        return needed

    async def _fetch_rcem(self, years: set[str]) -> dict[str, float]:
        found: dict[str, float] = {}
        try:
            async with async_get_clientsession(self.hass).get(
                RCEM_PAGE, timeout=ClientTimeout(total=45)
            ) as response:
                response.raise_for_status()
                page = await response.text()
            for year in sorted(years):
                try:
                    found.update(parse_rcem(page, int(year)))
                except ValueError as err:
                    _LOGGER.warning("Brak tabeli RCEm za %s: %s", year, err)
        except Exception as err:  # noqa: BLE001 - the missing prices are reported later
            _LOGGER.warning("Nie udało się pobrać archiwum RCEm: %s", err)
        return found

    async def _monthly_kwh(
        self, statistic_ids: set[str], first: str
    ) -> dict[str, dict[str, float]]:
        """Monthly energy in kWh from ``first`` for each statistic."""

        rows = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                rcem_statistics_start({first: 0.0}),
                None,
                statistic_ids,
                "month",
                {EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR},
                {"change"},
            )
        )
        return {
            statistic_id: {
                rcem_month_key(
                    datetime.fromtimestamp(float(row["start"]), WARSAW).date()
                ): float(row["change"] or 0.0)
                for row in rows.get(statistic_id, [])
            }
            for statistic_id in statistic_ids
        }

    async def _async_refresh(self) -> dict[str, Any]:
        settings = self._settings()
        invoice = parse_tranches(str(settings.get(CONF_DEPOSIT_TRANCHES) or ""))
        balance_month = (
            max(invoice) if invoice else str(settings[CONF_DEPOSIT_MONTH])[:7]
        )
        current = rcem_month_key(datetime.now(WARSAW).date())
        last = add_months(current, -1)
        if balance_month > last:
            raise ValueError(
                f"Miesiąc salda ({balance_month}) jeszcze się nie zakończył"
            )

        # The balance includes the tranche of balance_month, so 12 months
        # (balance_month-11..balance_month) are needed.
        first = min([add_months(balance_month, -11), *invoice])
        prices = await self._rcem_prices(first, last)
        mapped = {
            zone: str(settings[external_statistic_key(zone)])
            for zone in self.tariff.zones
        }
        export_id = str(settings[CONF_EXPORT_STATISTIC])
        exported = (await self._monthly_kwh({export_id}, first))[export_id]
        imported = await self._monthly_kwh(
            set(mapped.values()), add_months(balance_month, 1)
        )

        # Only the active energy price counts (it already includes excise);
        # the deposit does not pay for distribution or fixed fees.
        energy_prices = settings.get(CONF_CUSTOM_PRICES)
        if settings.get(CONF_PRICE_SOURCE) != PRICE_SOURCE_CUSTOM:
            energy_prices = self.coordinator.data.prices
        costs: dict[str, float] = {}
        for zone, statistic_id in mapped.items():
            price = price_for_zone(
                self.tariff,
                zone,
                custom_energy=energy_prices,
                distribution_net=self.coordinator.data.distribution_net,
                system_net=self.coordinator.data.system_total,
            ).energy
            for month, kwh in imported[statistic_id].items():
                costs[month] = costs.get(month, 0.0) + kwh * price

        # The tranche for the last finished month waits for its RCEm; the
        # month in progress is never assigned.
        in_range = {
            month: kwh
            for month, kwh in exported.items()
            if month <= last and (month in prices or month != last)
        }
        inflows = monthly_inflows(in_range, prices)
        if invoice:
            opening = opening_from_invoice(invoice, inflows)
        else:
            opening = opening_tranches(
                float(settings[CONF_DEPOSIT_BALANCE]), balance_month, inflows
            )
        if balance_month == last:
            tranches = [t for t in opening if t.remaining > 1e-9]
        else:
            tranches = simulate(
                opening, inflows, costs, add_months(balance_month, 1), last
            )

        # The current month has no RCEm yet; its inflow uses the latest known.
        inflow_so_far = (
            inflow(current, exported.get(current, 0.0), prices[max(prices)]).value
            if prices
            else None
        )
        return {
            "tranches": tranches,
            "cost": costs.get(current, 0.0),
            "inflow": inflow_so_far,
            "month": current,
            "source": "invoice" if invoice else "balance",
            "start_month": balance_month,
            "start_balance": sum(t.remaining for t in opening),
        }
