"""Configuration par l'interface pour HA Thermostat Split (une entrée = une pièce)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector

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
    CONF_MODULATION_INTERVAL,
    CONF_NAME,
    CONF_POWER,
    CONF_POWER_THRESHOLD,
    CONF_RESYNC,
    CONF_SENSOR_MAX_AGE,
    CONF_SENSORS,
    CONF_SPLIT,
    CONF_SPLIT_SETPOINT,
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


def _excluded_splits(hass: HomeAssistant, current_entry_id: str | None = None) -> list[str]:
    """Exclut les thermostats virtuels de cette intégration et les splits déjà attribués."""
    excluded: set[str] = set()
    ent_reg = er.async_get(hass)
    for ent in ent_reg.entities.values():
        if ent.platform == DOMAIN and ent.domain == "climate":
            excluded.add(ent.entity_id)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if current_entry_id is not None and entry.entry_id == current_entry_id:
            continue
        merged = {**entry.data, **entry.options}
        used_split = merged.get(CONF_SPLIT)
        if isinstance(used_split, str) and used_split:
            excluded.add(used_split)
    return sorted(excluded)


def _validate_split_choice(
    hass: HomeAssistant, split_entity: str | None, current_entry_id: str | None = None
) -> str | None:
    """Vérifie qu'on ne pilote ni un thermostat virtuel ni un split déjà géré ailleurs."""
    if not split_entity:
        return "invalid_split"
    ent_entry = er.async_get(hass).async_get(split_entity)
    if ent_entry is not None and ent_entry.platform == DOMAIN:
        return "self_split"
    for entry in hass.config_entries.async_entries(DOMAIN):
        if current_entry_id is not None and entry.entry_id == current_entry_id:
            continue
        merged = {**entry.data, **entry.options}
        if merged.get(CONF_SPLIT) == split_entity:
            return "already_configured_split"
    return None


def _build_user_schema(cur: dict[str, Any], excluded_splits: list[str]) -> vol.Schema:
    """Schéma ultra-simplifié pour l'ajout initial d'une pièce (4 champs essentiels)."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=cur.get(CONF_NAME, "Salon")): str,
            vol.Required(
                CONF_SPLIT, description={"suggested_value": cur.get(CONF_SPLIT)}
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="climate", exclude_entities=excluded_splits
                )
            ),
            vol.Required(
                CONF_SENSORS, description={"suggested_value": cur.get(CONF_SENSORS)}
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True)
            ),
            vol.Required(
                CONF_POWER, description={"suggested_value": cur.get(CONF_POWER)}
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
        }
    )


def _build_options_schema(cur: dict[str, Any], excluded_splits: list[str]) -> vol.Schema:
    """Schéma complet disponible dans les options (bouton 'Configurer')."""
    d = {**DEFAULTS, **cur}
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_SPLIT, description={"suggested_value": cur.get(CONF_SPLIT)}
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="climate", exclude_entities=excluded_splits
            )
        ),
        vol.Required(
            CONF_SENSORS, description={"suggested_value": cur.get(CONF_SENSORS)}
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True)
        ),
        vol.Required(
            CONF_POWER, description={"suggested_value": cur.get(CONF_POWER)}
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_POWER_THRESHOLD, default=d[CONF_POWER_THRESHOLD]): _num(1, 500, 1, "W"),
        vol.Required(CONF_MIN_TEMP, default=d[CONF_MIN_TEMP]): _num(5, 25, 0.5, "°C"),
        vol.Required(CONF_MAX_TEMP, default=d[CONF_MAX_TEMP]): _num(15, 30, 0.5, "°C"),
        vol.Required(CONF_ANTI_SPAM, default=d[CONF_ANTI_SPAM]): _num(15, 300, 5, "s"),
        vol.Required(CONF_MODULATION_INTERVAL, default=d[CONF_MODULATION_INTERVAL]): _num(60, 900, 30, "s"),
        vol.Required(CONF_INVERTER_MODE, default=d[CONF_INVERTER_MODE]): selector.BooleanSelector(),
        vol.Required(CONF_INVERTER_MIN_OFFSET, default=d[CONF_INVERTER_MIN_OFFSET]): _num(0.5, 3.0, 0.1, "°C"),
        vol.Required(CONF_INVERTER_MAX_OFFSET, default=d[CONF_INVERTER_MAX_OFFSET]): _num(1.5, 5.0, 0.5, "°C"),
        vol.Required(CONF_INVERTER_P_BAND, default=d[CONF_INVERTER_P_BAND]): _num(0.5, 4.0, 0.1, "°C"),
        vol.Required(CONF_LEARNING_ENABLED, default=d[CONF_LEARNING_ENABLED]): selector.BooleanSelector(),
        vol.Required(CONF_SPLIT_SETPOINT, default=d[CONF_SPLIT_SETPOINT]): _num(16, 31, 0.5, "°C"),
        vol.Required(CONF_MIN_ON, default=d[CONF_MIN_ON]): _num(0, 240, 1, "min"),
        vol.Required(CONF_MIN_OFF, default=d[CONF_MIN_OFF]): _num(0, 120, 1, "min"),
        vol.Required(CONF_COLD_TOL, default=d[CONF_COLD_TOL]): _num(0.1, 3, 0.1, "°C"),
        vol.Required(CONF_HOT_TOL, default=d[CONF_HOT_TOL]): _num(0, 3, 0.1, "°C"),
        vol.Required(CONF_MAX_OVERSHOOT, default=d[CONF_MAX_OVERSHOOT]): _num(0.2, 4, 0.1, "°C"),
        vol.Required(CONF_MAX_UNDERSHOOT, default=d[CONF_MAX_UNDERSHOOT]): _num(0.2, 5, 0.1, "°C"),
        vol.Required(CONF_SENSOR_MAX_AGE, default=d[CONF_SENSOR_MAX_AGE]): _num(5, 1440, 5, "min"),
        vol.Required(CONF_FAILSAFE, default=d[CONF_FAILSAFE]): _num(5, 600, 5, "min"),
        vol.Required(CONF_RESYNC, default=d[CONF_RESYNC]): _num(1, 120, 1, "min"),
    }
    return vol.Schema(fields)


class HAThermostatSplitConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Ajout d'une pièce (ultra simple : 4 champs)."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        excluded = _excluded_splits(self.hass)
        if user_input is not None:
            sensors = user_input.get(CONF_SENSORS)
            split_err = _validate_split_choice(self.hass, user_input.get(CONF_SPLIT))
            if split_err:
                errors[CONF_SPLIT] = split_err
            elif not sensors:
                errors[CONF_SENSORS] = "no_sensor"
            else:
                if isinstance(sensors, str):
                    user_input[CONF_SENSORS] = [sensors]
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=_build_user_schema(user_input or {}, excluded),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return HAThermostatSplitOptionsFlow()


class HAThermostatSplitOptionsFlow(config_entries.OptionsFlow):
    """Modification et personnalisation avancée des paramètres d'une pièce."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        excluded = _excluded_splits(self.hass, current_entry_id=self.config_entry.entry_id)
        if user_input is not None:
            sensors = user_input.get(CONF_SENSORS)
            split_err = _validate_split_choice(
                self.hass,
                user_input.get(CONF_SPLIT),
                current_entry_id=self.config_entry.entry_id,
            )
            if split_err:
                errors[CONF_SPLIT] = split_err
            elif not sensors:
                errors[CONF_SENSORS] = "no_sensor"
            elif user_input[CONF_MIN_TEMP] >= user_input[CONF_MAX_TEMP]:
                errors[CONF_MIN_TEMP] = "bad_range"
            elif user_input[CONF_INVERTER_MIN_OFFSET] > user_input[CONF_INVERTER_MAX_OFFSET]:
                errors[CONF_INVERTER_MIN_OFFSET] = "bad_offset_range"
            else:
                if isinstance(sensors, str):
                    user_input[CONF_SENSORS] = [sensors]
                return self.async_create_entry(title="", data=user_input)
            current = {**current, **user_input}

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(current, excluded),
            errors=errors,
        )
