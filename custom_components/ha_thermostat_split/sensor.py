"""Capteur de température moyenne de la pièce pour HA Thermostat Split."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .climate import compute_room_temperature
from .const import CONF_SENSOR_MAX_AGE, CONF_SENSORS, DEFAULTS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configure le capteur de température moyenne de la pièce."""
    async_add_entities([HAThermostatSplitRoomTempSensor(hass, entry)])


class HAThermostatSplitRoomTempSensor(SensorEntity):
    """Capteur exposant la température moyenne de la pièce calculée par le thermostat virtuel."""

    _attr_has_entity_name = True
    _attr_translation_key = "room_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 2
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        cfg = {**DEFAULTS, **entry.data, **entry.options}
        raw_sensors = cfg.get(CONF_SENSORS, [])
        if isinstance(raw_sensors, str):
            self._sensors = [raw_sensors]
        else:
            self._sensors = [str(s) for s in raw_sensors]
        self._sensor_max_age = float(cfg[CONF_SENSOR_MAX_AGE])

        self._attr_unique_id = f"{entry.entry_id}_room_temperature"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )
        self._attr_native_value: float | None = None
        self._used: list[str] = []
        self._ignored: dict[str, str] = {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "capteurs_utilises": self._used,
            "capteurs_ignores": self._ignored,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        callbacks: list = (
            self.hass.data.setdefault(DOMAIN, {})
            .setdefault("temp_callbacks", {})
            .setdefault(self._entry.entry_id, [])
        )
        callbacks.append(self._on_temp_update)

        @callback
        def _remove_cb() -> None:
            if self._on_temp_update in callbacks:
                callbacks.remove(self._on_temp_update)

        self.async_on_remove(_remove_cb)

        thermostat = (
            self.hass.data.get(DOMAIN, {})
            .get("entities", {})
            .get(self._entry.entry_id)
        )
        if thermostat is not None and thermostat.current_temperature is not None:
            self._attr_native_value = thermostat.current_temperature
            self._used = list(getattr(thermostat, "_used", []))
            self._ignored = dict(getattr(thermostat, "_ignored", {}))
        else:
            avg_temp, used, ignored = compute_room_temperature(
                self.hass, self._sensors, self._sensor_max_age, dt_util.utcnow()
            )
            self._attr_native_value = avg_temp
            self._used = used
            self._ignored = ignored

    @callback
    def _on_temp_update(
        self,
        temp: float | None,
        used: list[str],
        ignored: dict[str, str],
    ) -> None:
        self._attr_native_value = temp
        self._used = used
        self._ignored = ignored
        self.async_write_ha_state()
