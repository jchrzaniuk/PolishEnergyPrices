"""Config flow for Polish Energy Prices."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    CONF_CUSTOM_PRICES,
    CONF_DAY_HOURS,
    CONF_EXPORT_CORRECTION,
    CONF_EXPORT_SETTLEMENT,
    CONF_EXPORT_STATISTIC,
    CONF_EXPORT_STATISTICS,
    CONF_EXTERNAL_STATISTIC_PREFIX,
    CONF_EXTERNAL_STATISTICS,
    CONF_METER_CLOCK,
    CONF_OPERATOR,
    CONF_PRICE_SOURCE,
    CONF_TARIFF,
    DEFAULT_EXPORT_CORRECTION,
    DOMAIN,
    EXPORT_SETTLEMENT_OFF,
    EXPORT_SETTLEMENT_RCE,
    EXPORT_SETTLEMENT_RCEM,
    METER_CLOCK_FIXED_WINTER,
    METER_CLOCK_LOCAL,
    PRICE_SOURCE_CUSTOM,
    PRICE_SOURCE_REGULATED,
    PRICE_SOURCE_TAURON_G13S,
    PRICE_SOURCE_TAURON_G14DYNAMIC,
    external_statistic_key,
)
from .tariff import (
    G13S_BASE_ZONES,
    G13S_PERIODS,
    OPERATOR_NAMES,
    get_tariff,
    groups_for,
    parse_day_hours,
)

G13S_PERIOD_NAMES = {
    "zima_dzien_roboczy": "zima — dzień roboczy",
    "zima_dzien_wolny": "zima — dzień wolny",
    "lato_dzien_roboczy": "lato — dzień roboczy",
    "lato_dzien_wolny": "lato — dzień wolny",
}


def _select(options: list[SelectOptionDict]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _price_source_selector(operator: str, group: str) -> SelectSelector:
    if operator == "tauron" and group.lower() == "g13s":
        return _select(
            [
                SelectOptionDict(
                    value=PRICE_SOURCE_CUSTOM,
                    label="mój cennik — ceny brutto z umowy",
                ),
                SelectOptionDict(
                    value=PRICE_SOURCE_TAURON_G13S,
                    label="najnowsza oferta G13s TAURON — automatycznie",
                ),
            ]
        )
    if operator == "tauron" and group.lower() == "g14dynamic":
        return _select(
            [
                SelectOptionDict(
                    value=PRICE_SOURCE_TAURON_G14DYNAMIC,
                    label="oficjalny cennik Prąd ze zmienną dystrybucją — automatycznie",
                ),
                SelectOptionDict(
                    value=PRICE_SOURCE_CUSTOM,
                    label="mój cennik — ceny brutto z umowy",
                ),
            ]
        )
    return _select(
        [
            SelectOptionDict(
                value=PRICE_SOURCE_REGULATED,
                label="sprzedawca z urzędu — automatyczne ceny URE",
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
            SelectOptionDict(
                value=METER_CLOCK_LOCAL, label="czas lokalny (licznik AMI)"
            ),
            SelectOptionDict(
                value=METER_CLOCK_FIXED_WINTER,
                label="stały czas zimowy (starszy licznik)",
            ),
        ]
    )


def _export_settlement_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                EXPORT_SETTLEMENT_OFF,
                EXPORT_SETTLEMENT_RCE,
                EXPORT_SETTLEMENT_RCEM,
            ],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="export_settlement",
        )
    )


def _parse_export_correction(value: Any) -> float | None:
    """Parse the deposit correction factor, accepting a Polish decimal comma."""

    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return parsed if 0.5 <= parsed <= 2.0 else None


def _invalid_export_correction(value: Any) -> bool:
    """Return whether the deposit correction factor is out of the 0.5-2.0 range."""

    return _parse_export_correction(value) is None


_EXPORT_CORRECTION_OPTIONS = ("1.23", "1.0")


def _export_correction_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=list(_EXPORT_CORRECTION_OPTIONS),
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="export_correction",
        )
    )


def _export_correction_default(value: Any) -> str:
    """Match a stored correction factor to a list option, defaulting to 1.0."""

    parsed = _parse_export_correction(value)
    for option in _EXPORT_CORRECTION_OPTIONS:
        if parsed == float(option):
            return option
    return "1.0"


def _operator_schema(default: str | None = None) -> vol.Schema:
    marker = (
        vol.Required(CONF_OPERATOR, default=default)
        if default
        else vol.Required(CONF_OPERATOR)
    )
    return vol.Schema(
        {
            marker: _select(
                [
                    SelectOptionDict(value=key, label=name)
                    for key, name in OPERATOR_NAMES.items()
                ]
            )
        }
    )


def _tariff_schema(operator: str, defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    groups = groups_for(operator)
    schema: dict[Any, Any] = {
        vol.Required(
            CONF_TARIFF, default=defaults.get(CONF_TARIFF, groups[0])
        ): _select(
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
        schema[
            vol.Optional(
                CONF_DAY_HOURS,
                default=defaults.get(CONF_DAY_HOURS, "6-13,15-22"),
            )
        ] = TextSelector()
    return vol.Schema(schema)


def _source_schema(
    operator: str,
    group: str,
    default: str = PRICE_SOURCE_REGULATED,
    external_statistics: bool = False,
    export_settlement: str = EXPORT_SETTLEMENT_OFF,
    export_statistics: bool = False,
    export_correction: str = str(DEFAULT_EXPORT_CORRECTION),
) -> vol.Schema:
    tariff = get_tariff(operator, group)
    schema: dict[Any, Any] = {
        vol.Required(CONF_PRICE_SOURCE, default=default): _price_source_selector(
            operator, group
        )
    }
    if tariff.external_statistics_supported:
        schema[
            vol.Required(CONF_EXTERNAL_STATISTICS, default=external_statistics)
        ] = BooleanSelector()
    schema[
        vol.Required(CONF_EXPORT_SETTLEMENT, default=export_settlement)
    ] = _export_settlement_selector()
    schema[
        vol.Required(CONF_EXPORT_STATISTICS, default=export_statistics)
    ] = BooleanSelector()
    schema[
        vol.Required(CONF_EXPORT_CORRECTION, default=export_correction)
    ] = _export_correction_selector()
    return vol.Schema(schema)


def _export_statistic_schema(
    statistic_options: list[SelectOptionDict],
    default: str | None = None,
) -> vol.Schema:
    """Select one cumulative energy-exported statistic for RCEm compensation."""

    marker = (
        vol.Required(CONF_EXPORT_STATISTIC, default=default)
        if default
        else vol.Required(CONF_EXPORT_STATISTIC)
    )
    return vol.Schema({marker: _select(statistic_options)})


def _mapped_consumption_statistics(data: dict[str, Any]) -> set[str]:
    """Return cumulative energy statistics mapped to a tariff zone.

    Only meaningful while the consumption-cost bridge is enabled: a
    disabled bridge can leave stale zone mappings sitting in options, and
    those must not collide with the export statistic selection.
    """

    if not data.get(CONF_EXTERNAL_STATISTICS, False):
        return set()
    return {
        str(value)
        for key, value in data.items()
        if key.startswith(CONF_EXTERNAL_STATISTIC_PREFIX) and value
    }


def _prices_schema(
    operator: str,
    group: str,
    defaults: dict[str, float] | None = None,
) -> vol.Schema:
    tariff = get_tariff(operator, group)
    defaults = defaults or dict(tariff.energy_gross)
    if tariff.dynamic_zone_source:
        default_price = next(iter(defaults.values()))
        return vol.Schema(
            {
                vol.Required("calodobowa", default=default_price): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=10,
                        step=0.0001,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="PLN/kWh",
                    )
                )
            }
        )
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


def _custom_price_values(
    operator: str, group: str, values: dict[str, float]
) -> dict[str, float]:
    """Expand the single sales price used by a dynamic distribution tariff."""

    tariff = get_tariff(operator, group)
    if not tariff.dynamic_zone_source:
        return values
    price = float(values["calodobowa"])
    return {zone: price for zone in tariff.zones}


def _g13s_prices_schema(
    period: str,
    defaults: dict[str, float] | None = None,
) -> vol.Schema:
    """Collect one three-zone G13s price set on a compact form."""

    tariff = get_tariff("tauron", "G13s")
    defaults = defaults or dict(tariff.energy_gross)
    return vol.Schema(
        {
            vol.Required(
                zone,
                default=defaults.get(
                    f"{period}_{zone}", tariff.energy_gross[f"{period}_{zone}"]
                ),
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=10))
            for zone in G13S_BASE_ZONES
        }
    )


def _external_statistics_schema(
    operator: str,
    group: str,
    statistic_options: list[SelectOptionDict],
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Select one cumulative energy statistic for every tariff zone."""

    defaults = defaults or {}
    schema: dict[Any, Any] = {}
    if operator == "tauron" and group.lower() == "g13s":
        key = external_statistic_key("g13s")
        marker = (
            vol.Required(key, default=defaults[key])
            if defaults.get(key)
            else vol.Required(key)
        )
        return vol.Schema({marker: _select(statistic_options)})
    for zone in get_tariff(operator, group).zones:
        key = external_statistic_key(zone)
        marker = (
            vol.Required(key, default=defaults[key])
            if defaults.get(key)
            else vol.Required(key)
        )
        schema[marker] = _select(statistic_options)
    return vol.Schema(schema)


async def _energy_statistic_options(
    hass: HomeAssistant,
) -> list[SelectOptionDict]:
    """Return cumulative energy statistics as ordinary select options."""

    recorder = get_instance(hass)
    metadata = await recorder.async_add_executor_job(list_statistic_ids, hass)
    options: list[SelectOptionDict] = []
    for item in metadata:
        if item.get("unit_class") != EnergyConverter.UNIT_CLASS or not item.get(
            "has_sum"
        ):
            continue
        statistic_id = str(item["statistic_id"])
        name = item.get("name")
        label = (
            f"{name} — {statistic_id}"
            if name and str(name) != statistic_id
            else statistic_id
        )
        options.append(SelectOptionDict(value=statistic_id, label=label))
    return sorted(options, key=lambda option: option["label"].casefold())


class PolishEnergyPriceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the integration configuration flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._g13s_period_index = 0
        self._g13s_prices: dict[str, float] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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

        errors: dict[str, str] = {}
        if user_input is not None:
            if _invalid_export_correction(user_input.get(CONF_EXPORT_CORRECTION)):
                errors[CONF_EXPORT_CORRECTION] = "invalid_export_correction"
            if (
                user_input.get(CONF_EXPORT_STATISTICS)
                and user_input.get(CONF_EXPORT_SETTLEMENT) != EXPORT_SETTLEMENT_RCEM
            ):
                errors[CONF_EXPORT_STATISTICS] = "export_statistics_requires_rcem"
            if not errors:
                tariff = get_tariff(
                    self._data[CONF_OPERATOR], self._data[CONF_TARIFF]
                )
                if not tariff.external_statistics_supported:
                    user_input[CONF_EXTERNAL_STATISTICS] = False
                user_input[CONF_EXPORT_CORRECTION] = _parse_export_correction(
                    user_input[CONF_EXPORT_CORRECTION]
                )
                self._data.update(user_input)
                if user_input[CONF_PRICE_SOURCE] == PRICE_SOURCE_CUSTOM:
                    if self._data[CONF_TARIFF].lower() == "g13s":
                        return await self.async_step_g13s_custom_prices()
                    return await self.async_step_custom_prices()
                return await self._next_after_prices()
        operator = self._data[CONF_OPERATOR]
        group = self._data[CONF_TARIFF]
        default_source = (
            PRICE_SOURCE_CUSTOM
            if operator == "tauron" and group.lower() == "g13s"
            else (
                PRICE_SOURCE_TAURON_G14DYNAMIC
                if operator == "tauron" and group.lower() == "g14dynamic"
                else PRICE_SOURCE_REGULATED
            )
        )
        fields = user_input or {}
        return self.async_show_form(
            step_id="energy",
            data_schema=_source_schema(
                operator,
                group,
                default=fields.get(CONF_PRICE_SOURCE, default_source),
                external_statistics=fields.get(CONF_EXTERNAL_STATISTICS, False),
                export_settlement=fields.get(
                    CONF_EXPORT_SETTLEMENT, EXPORT_SETTLEMENT_OFF
                ),
                export_statistics=fields.get(CONF_EXPORT_STATISTICS, False),
                export_correction=_export_correction_default(
                    fields.get(CONF_EXPORT_CORRECTION, DEFAULT_EXPORT_CORRECTION)
                ),
            ),
            errors=errors,
        )

    async def async_step_custom_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect gross prices from the user's contract."""

        if user_input is not None:
            self._data[CONF_CUSTOM_PRICES] = _custom_price_values(
                self._data[CONF_OPERATOR], self._data[CONF_TARIFF], user_input
            )
            return await self._next_after_prices()
        return self.async_show_form(
            step_id="custom_prices",
            data_schema=_prices_schema(
                self._data[CONF_OPERATOR], self._data[CONF_TARIFF]
            ),
        )

    async def async_step_g13s_custom_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect four seasonal/day-type sets of gross G13s prices."""

        period = G13S_PERIODS[self._g13s_period_index]
        if user_input is not None:
            self._g13s_prices.update(
                {f"{period}_{zone}": value for zone, value in user_input.items()}
            )
            self._g13s_period_index += 1
            if self._g13s_period_index == len(G13S_PERIODS):
                self._data[CONF_CUSTOM_PRICES] = self._g13s_prices
                return await self._next_after_prices()
            period = G13S_PERIODS[self._g13s_period_index]
        return self.async_show_form(
            step_id="g13s_custom_prices",
            data_schema=_g13s_prices_schema(period),
            description_placeholders={"period": G13S_PERIOD_NAMES[period]},
        )

    async def _next_after_prices(self) -> ConfigFlowResult:
        if self._data.get(CONF_EXTERNAL_STATISTICS, False):
            return await self.async_step_external_statistics()
        return await self._after_external_statistics()

    async def async_step_external_statistics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Map external cumulative energy statistics to tariff zones."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if len(set(user_input.values())) != len(user_input):
                errors["base"] = "duplicate_statistics"
            else:
                self._data.update(user_input)
                return await self._after_external_statistics()
        statistic_options = await _energy_statistic_options(self.hass)
        if not statistic_options:
            errors["base"] = "no_energy_statistics"
        return self.async_show_form(
            step_id="external_statistics",
            data_schema=_external_statistics_schema(
                self._data[CONF_OPERATOR],
                self._data[CONF_TARIFF],
                statistic_options,
                user_input,
            ),
            errors=errors,
        )

    async def _after_external_statistics(self) -> ConfigFlowResult:
        if self._data.get(CONF_EXPORT_STATISTICS, False):
            return await self.async_step_export_statistic()
        return self._finish()

    async def async_step_export_statistic(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the cumulative energy-exported statistic for RCEm compensation."""

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = str(user_input[CONF_EXPORT_STATISTIC])
            if selected in _mapped_consumption_statistics(self._data):
                errors["base"] = "export_statistic_duplicate"
            else:
                self._data[CONF_EXPORT_STATISTIC] = selected
                return self._finish()
        statistic_options = await _energy_statistic_options(self.hass)
        if not statistic_options:
            errors["base"] = "no_energy_statistics"
        return self.async_show_form(
            step_id="export_statistic",
            data_schema=_export_statistic_schema(
                statistic_options, self._data.get(CONF_EXPORT_STATISTIC)
            ),
            errors=errors,
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
        self._g13s_period_index = 0
        self._g13s_prices: dict[str, float] = {}

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
            if _invalid_export_correction(user_input.get(CONF_EXPORT_CORRECTION)):
                errors[CONF_EXPORT_CORRECTION] = "invalid_export_correction"
            if (
                user_input.get(CONF_EXPORT_STATISTICS)
                and user_input.get(CONF_EXPORT_SETTLEMENT) != EXPORT_SETTLEMENT_RCEM
            ):
                errors[CONF_EXPORT_STATISTICS] = "export_statistics_requires_rcem"
            if not errors:
                self._options = user_input
                self._options[CONF_EXPORT_CORRECTION] = _parse_export_correction(
                    user_input[CONF_EXPORT_CORRECTION]
                )
                if not get_tariff(operator, group).external_statistics_supported:
                    self._options[CONF_EXTERNAL_STATISTICS] = False
                if user_input[CONF_PRICE_SOURCE] == PRICE_SOURCE_CUSTOM:
                    if group.lower() == "g13s":
                        return await self.async_step_g13s_custom_prices()
                    return await self.async_step_custom_prices()
                return await self._next_after_prices()

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_PRICE_SOURCE,
                default=current.get(
                    CONF_PRICE_SOURCE,
                    (
                        PRICE_SOURCE_CUSTOM
                        if operator == "tauron" and group.lower() == "g13s"
                        else (
                            PRICE_SOURCE_TAURON_G14DYNAMIC
                            if operator == "tauron"
                            and group.lower() == "g14dynamic"
                            else PRICE_SOURCE_REGULATED
                        )
                    ),
                ),
            ): _price_source_selector(operator, group),
            vol.Required(
                CONF_METER_CLOCK,
                default=current.get(CONF_METER_CLOCK, METER_CLOCK_LOCAL),
            ): _meter_clock_selector(),
            vol.Required(
                CONF_EXPORT_SETTLEMENT,
                default=current.get(CONF_EXPORT_SETTLEMENT, EXPORT_SETTLEMENT_OFF),
            ): _export_settlement_selector(),
            vol.Required(
                CONF_EXPORT_STATISTICS,
                default=current.get(CONF_EXPORT_STATISTICS, False),
            ): BooleanSelector(),
            vol.Required(
                CONF_EXPORT_CORRECTION,
                default=_export_correction_default(
                    current.get(CONF_EXPORT_CORRECTION, DEFAULT_EXPORT_CORRECTION)
                ),
            ): _export_correction_selector(),
        }
        if get_tariff(operator, group).external_statistics_supported:
            schema[
                vol.Required(
                    CONF_EXTERNAL_STATISTICS,
                    default=current.get(CONF_EXTERNAL_STATISTICS, False),
                )
            ] = BooleanSelector()
        if operator == "enea" and group == "G12":
            schema[
                vol.Optional(
                    CONF_DAY_HOURS,
                    default=current.get(CONF_DAY_HOURS, "6-13,15-22"),
                )
            ] = TextSelector()
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_custom_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit gross contract prices."""

        if user_input is not None:
            self._options[CONF_CUSTOM_PRICES] = _custom_price_values(
                self.config_entry.data[CONF_OPERATOR],
                self.config_entry.data[CONF_TARIFF],
                user_input,
            )
            return await self._next_after_prices()
        return self.async_show_form(
            step_id="custom_prices",
            data_schema=_prices_schema(
                self.config_entry.data[CONF_OPERATOR],
                self.config_entry.data[CONF_TARIFF],
                self.config_entry.options.get(CONF_CUSTOM_PRICES),
            ),
        )

    async def async_step_g13s_custom_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit four seasonal/day-type sets of gross G13s prices."""

        period = G13S_PERIODS[self._g13s_period_index]
        if user_input is not None:
            self._g13s_prices.update(
                {f"{period}_{zone}": value for zone, value in user_input.items()}
            )
            self._g13s_period_index += 1
            if self._g13s_period_index == len(G13S_PERIODS):
                self._options[CONF_CUSTOM_PRICES] = self._g13s_prices
                return await self._next_after_prices()
            period = G13S_PERIODS[self._g13s_period_index]
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="g13s_custom_prices",
            data_schema=_g13s_prices_schema(period, current.get(CONF_CUSTOM_PRICES)),
            description_placeholders={"period": G13S_PERIOD_NAMES[period]},
        )

    async def _next_after_prices(self) -> ConfigFlowResult:
        if self._options.get(CONF_EXTERNAL_STATISTICS, False):
            return await self.async_step_external_statistics()
        return await self._after_external_statistics()

    async def async_step_external_statistics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit mappings from cumulative energy statistics to tariff zones."""

        errors: dict[str, str] = {}
        if user_input is not None:
            if len(set(user_input.values())) != len(user_input):
                errors["base"] = "duplicate_statistics"
            else:
                self._options.update(user_input)
                return await self._after_external_statistics()
        current = {
            **self.config_entry.data,
            **self.config_entry.options,
            **self._options,
        }
        statistic_options = await _energy_statistic_options(self.hass)
        if not statistic_options:
            errors["base"] = "no_energy_statistics"
        return self.async_show_form(
            step_id="external_statistics",
            data_schema=_external_statistics_schema(
                self.config_entry.data[CONF_OPERATOR],
                self.config_entry.data[CONF_TARIFF],
                statistic_options,
                current if user_input is None else user_input,
            ),
            errors=errors,
        )

    async def _after_external_statistics(self) -> ConfigFlowResult:
        if self._options.get(CONF_EXPORT_STATISTICS, False):
            return await self.async_step_export_statistic()
        return self.async_create_entry(data=self._options)

    async def async_step_export_statistic(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the cumulative energy-exported statistic for RCEm compensation."""

        errors: dict[str, str] = {}
        current = {
            **self.config_entry.data,
            **self.config_entry.options,
            **self._options,
        }
        if user_input is not None:
            selected = str(user_input[CONF_EXPORT_STATISTIC])
            if selected in _mapped_consumption_statistics(current):
                errors["base"] = "export_statistic_duplicate"
            else:
                self._options[CONF_EXPORT_STATISTIC] = selected
                return self.async_create_entry(data=self._options)
        statistic_options = await _energy_statistic_options(self.hass)
        if not statistic_options:
            errors["base"] = "no_energy_statistics"
        return self.async_show_form(
            step_id="export_statistic",
            data_schema=_export_statistic_schema(
                statistic_options, current.get(CONF_EXPORT_STATISTIC)
            ),
            errors=errors,
        )
