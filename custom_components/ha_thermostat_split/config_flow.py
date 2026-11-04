"""Configuration par l'interface pour HA Thermostat Split (une entrée = une pièce)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ANTI_SPAM,
    CONF_COLD_TOL,
    CONF_FAILSAFE,
    CONF_HOT_TOL,
    CONF_INVERTER_MAX_OFFSET,
    CONF_INVERTER_MIN_OFFSET,
    CONF_INVERTER_MODE,
    CONF_INVERTER_P_BAND,
    CONF_LEARNING_ENABLED,
    CONF_MAX_OVERSHOOT,
    CONF_MAX_TEMP,
    CONF_MAX_UNDERSHOOT,
    CONF_MIN_OFF,
    CONF_MIN_ON,
    CONF_MIN_TEMP,
    CONF_NAME,
    CONF_POWER,
    CONF_POWER_THRESHOLD,
    CONF_RESYNC,
    CONF_SENSOR_MAX_AGE,
    CONF_SENSORS,
    CONF_SPLIT,
    CONF_SPLIT_SETPOINT,
    CONF_TARGET_DEFAULT,
    DEFAULTS,
    DOMAIN,
)


def _num(min_: float, max_: float, step: float, unit: str | None = None) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=min_,
            max=max_,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _build_schema(cur: dict[str, Any], with_name: bool) -> vol.Schema:
    d = {**DEFAULTS, **cur}
    fields: dict[Any, Any] = {}
    if with_name:
        fields[vol.Required(CONF_NAME, default=cur.get(CONF_NAME, "Salon"))] = str
    fields[
        vol.Required(CONF_SPLIT, description={"suggested_value": cur.get(CONF_SPLIT)})
    ] = selector.EntitySelector(selector.EntitySelectorConfig(domain="climate"))
    fields[
        vol.Required(CONF_SENSORS, description={"suggested_value": cur.get(CONF_SENSORS)})
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", multiple=True)
    )
    fields[
        vol.Required(CONF_POWER, description={"suggested_value": cur.get(CONF_POWER)})
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )
    fields[vol.Required(CONF_TARGET_DEFAULT, default=d[CONF_TARGET_DEFAULT])] = _num(10, 30, 0.5, "°C")
    fields[vol.Required(CONF_MIN_TEMP, default=d[CONF_MIN_TEMP])] = _num(5, 25, 0.5, "°C")
    fields[vol.Required(CONF_MAX_TEMP, default=d[CONF_MAX_TEMP])] = _num(15, 30, 0.5, "°C")
    fields[vol.Required(CONF_ANTI_SPAM, default=d[CONF_ANTI_SPAM])] = _num(15, 300, 5, "s")
    fields[vol.Required(CONF_INVERTER_MODE, default=d[CONF_INVERTER_MODE])] = selector.BooleanSelector()
    fields[vol.Required(CONF_INVERTER_MIN_OFFSET, default=d[CONF_INVERTER_MIN_OFFSET])] = _num(0.0, 2.5, 0.1, "°C")
    fields[vol.Required(CONF_INVERTER_MAX_OFFSET, default=d[CONF_INVERTER_MAX_OFFSET])] = _num(1.0, 5.0, 0.5, "°C")
    fields[vol.Required(CONF_INVERTER_P_BAND, default=d[CONF_INVERTER_P_BAND])] = _num(0.5, 4.0, 0.1, "°C")
    fields[vol.Required(CONF_LEARNING_ENABLED, default=d[CONF_LEARNING_ENABLED])] = selector.BooleanSelector()
    fields[vol.Required(CONF_SPLIT_SETPOINT, default=d[CONF_SPLIT_SETPOINT])] = _num(16, 31, 0.5, "°C")
    fields[vol.Required(CONF_MIN_ON, default=d[CONF_MIN_ON])] = _num(0, 240, 1, "min")
    fields[vol.Required(CONF_MIN_OFF, default=d[CONF_MIN_OFF])] = _num(0, 120, 1, "min")
    fields[vol.Required(CONF_COLD_TOL, default=d[CONF_COLD_TOL])] = _num(0.1, 3, 0.1, "°C")
    fields[vol.Required(CONF_HOT_TOL, default=d[CONF_HOT_TOL])] = _num(0, 3, 0.1, "°C")
    fields[vol.Required(CONF_MAX_OVERSHOOT, default=d[CONF_MAX_OVERSHOOT])] = _num(0.2, 4, 0.1, "°C")
    fields[vol.Required(CONF_MAX_UNDERSHOOT, default=d[CONF_MAX_UNDERSHOOT])] = _num(0.2, 5, 0.1, "°C")
    fields[vol.Required(CONF_POWER_THRESHOLD, default=d[CONF_POWER_THRESHOLD])] = _num(1, 500, 1, "W")
    fields[vol.Required(CONF_SENSOR_MAX_AGE, default=d[CONF_SENSOR_MAX_AGE])] = _num(5, 1440, 5, "min")
    fields[vol.Required(CONF_FAILSAFE, default=d[CONF_FAILSAFE])] = _num(5, 600, 5, "min")
    fields[vol.Required(CONF_RESYNC, default=d[CONF_RESYNC])] = _num(1, 120, 1, "min")
    return vol.Schema(fields)


class HAThermostatSplitConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Ajout d'une pièce."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_SENSORS):
                errors[CONF_SENSORS] = "no_sensor"
            elif user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
                errors[CONF_MIN_TEMP] = "bad_range"
            else:
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(user_input or {}, with_name=True),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return HAThermostatSplitOptionsFlow()


class HAThermostatSplitOptionsFlow(config_entries.OptionsFlow):
    """Modification des paramètres d'une pièce."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            if not user_input.get(CONF_SENSORS):
                errors[CONF_SENSORS] = "no_sensor"
            elif user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
                errors[CONF_MIN_TEMP] = "bad_range"
            else:
                return self.async_create_entry(title="", data=user_input)
            current = {**current, **user_input}
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(current, with_name=False),
            errors=errors,
        )
