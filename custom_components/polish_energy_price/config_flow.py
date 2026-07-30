"""Config flow for Polish Energy Price."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_CUSTOM_PRICES,
    CONF_DAY_HOURS,
    CONF_METER_CLOCK,
    CONF_OPERATOR,
    CONF_PRICE_SOURCE,
    CONF_TARIFF,
    DOMAIN,
    METER_CLOCK_FIXED_WINTER,
    METER_CLOCK_LOCAL,
    PRICE_SOURCE_CUSTOM,
    PRICE_SOURCE_REGULATED,
)
from .tariff import OPERATOR_NAMES, get_tariff, groups_for, parse_day_hours


def _select(options: list[SelectOptionDict]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _price_source_selector() -> SelectSelector:
    return _select(
        [
            SelectOptionDict(
                value=PRICE_SOURCE_REGULATED,
                label="sprzedawca z urzędu — cennik regulowany 2026",
            ),
            SelectOptionDict(
                value=PRICE_SOURCE_CUSTOM,
                label="własny sprzedawca — ceny brutto z umowy",
            ),
        ]
    )


def _meter_clock_selector() -> SelectSelector:
    return _select(
        [
            SelectOptionDict(value=METER_CLOCK_LOCAL, label="czas lokalny (licznik AMI)"),
            SelectOptionDict(
                value=METER_CLOCK_FIXED_WINTER,
                label="stały czas zimowy (starszy licznik)",
            ),
        ]
    )


def _operator_schema(default: str | None = None) -> vol.Schema:
    marker = (
        vol.Required(CONF_OPERATOR, default=default)
        if default
        else vol.Required(CONF_OPERATOR)
    )
    return vol.Schema(
        {
            marker: _select(
                [SelectOptionDict(value=key, label=name) for key, name in OPERATOR_NAMES.items()]
            )
        }
    )


def _tariff_schema(operator: str, defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    groups = groups_for(operator)
    schema: dict[Any, Any] = {
        vol.Required(CONF_TARIFF, default=defaults.get(CONF_TARIFF, groups[0])): _select(
            [
                SelectOptionDict(
                    value=group,
                    label=f"{group} — {get_tariff(operator, group).description}",
                )
                for group in groups
            ]
        ),
        vol.Required(
            CONF_METER_CLOCK,
            default=defaults.get(CONF_METER_CLOCK, METER_CLOCK_LOCAL),
        ): _meter_clock_selector(),
    }
    if operator == "enea":
        schema[vol.Optional(
            CONF_DAY_HOURS,
            default=defaults.get(CONF_DAY_HOURS, "6-13,15-22"),
        )] = TextSelector()
    return vol.Schema(schema)


def _source_schema(default: str = PRICE_SOURCE_REGULATED) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PRICE_SOURCE, default=default): _price_source_selector()
        }
    )


def _prices_schema(
    operator: str,
    group: str,
    defaults: dict[str, float] | None = None,
) -> vol.Schema:
    tariff = get_tariff(operator, group)
    defaults = defaults or dict(tariff.energy_gross)
    return vol.Schema(
        {
            vol.Required(
                zone, default=defaults.get(zone, tariff.energy_gross[zone])
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=10,
                    step=0.0001,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="PLN/kWh",
                )
            )
            for zone in tariff.zones
        }
    )


class PolishEnergyPriceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the integration configuration flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select an OSD."""

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_tariff()
        return self.async_show_form(step_id="user", data_schema=_operator_schema())

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a tariff and meter clock."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if self._data[CONF_OPERATOR] == "enea" and user_input.get(CONF_DAY_HOURS):
                try:
                    parse_day_hours(user_input[CONF_DAY_HOURS])
                except ValueError:
                    errors[CONF_DAY_HOURS] = "invalid_hours"
            if not errors:
                if user_input.get(CONF_TARIFF) != "G12":
                    user_input.pop(CONF_DAY_HOURS, None)
                self._data.update(user_input)
                return await self.async_step_energy()
        return self.async_show_form(
            step_id="tariff",
            data_schema=_tariff_schema(self._data[CONF_OPERATOR], user_input),
            errors=errors,
        )

    async def async_step_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the energy seller price source."""

        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_PRICE_SOURCE] == PRICE_SOURCE_CUSTOM:
                return await self.async_step_custom_prices()
            return self._finish()
        return self.async_show_form(step_id="energy", data_schema=_source_schema())

    async def async_step_custom_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect gross prices from the user's contract."""

        if user_input is not None:
            self._data[CONF_CUSTOM_PRICES] = user_input
            return self._finish()
        return self.async_show_form(
            step_id="custom_prices",
            data_schema=_prices_schema(self._data[CONF_OPERATOR], self._data[CONF_TARIFF]),
        )

    def _finish(self) -> ConfigFlowResult:
        operator = self._data[CONF_OPERATOR]
        group = self._data[CONF_TARIFF]
        return self.async_create_entry(
            title=f"{OPERATOR_NAMES[operator]} {group}",
            data=self._data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Create the options flow."""

        return PolishEnergyPriceOptionsFlow()


class PolishEnergyPriceOptionsFlow(OptionsFlowWithReload):
    """Change energy prices, ENEA hours, and meter clock behavior."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit general options."""

        operator = self.config_entry.data[CONF_OPERATOR]
        group = self.config_entry.data[CONF_TARIFF]
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            if operator == "enea" and group == "G12" and user_input.get(CONF_DAY_HOURS):
                try:
                    parse_day_hours(user_input[CONF_DAY_HOURS])
                except ValueError:
                    errors[CONF_DAY_HOURS] = "invalid_hours"
            if not errors:
                self._options = user_input
                if user_input[CONF_PRICE_SOURCE] == PRICE_SOURCE_CUSTOM:
                    return await self.async_step_custom_prices()
                return self.async_create_entry(data=self._options)

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_PRICE_SOURCE,
                default=current.get(CONF_PRICE_SOURCE, PRICE_SOURCE_REGULATED),
            ): _price_source_selector(),
            vol.Required(
                CONF_METER_CLOCK,
                default=current.get(CONF_METER_CLOCK, METER_CLOCK_LOCAL),
            ): _meter_clock_selector(),
        }
        if operator == "enea" and group == "G12":
            schema[vol.Optional(
                CONF_DAY_HOURS,
                default=current.get(CONF_DAY_HOURS, "6-13,15-22"),
            )] = TextSelector()
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_custom_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit gross contract prices."""

        if user_input is not None:
            self._options[CONF_CUSTOM_PRICES] = user_input
            return self.async_create_entry(data=self._options)
        return self.async_show_form(
            step_id="custom_prices",
            data_schema=_prices_schema(
                self.config_entry.data[CONF_OPERATOR],
                self.config_entry.data[CONF_TARIFF],
                self.config_entry.options.get(CONF_CUSTOM_PRICES),
            ),
        )
