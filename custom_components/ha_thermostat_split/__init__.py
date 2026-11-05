"""HA Thermostat Split : thermostat virtuel par pièce pour splits inverter."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_INVERTER_MIN_OFFSET, CONF_MAX_OVERSHOOT, DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE]


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migre automatiquement les anciennes entrées vers les nouveaux seuils anti-yoyo."""
    if config_entry.version < 2:
        new_data = {**config_entry.data}
        new_options = {**config_entry.options}
        for store in (new_data, new_options):
            if CONF_INVERTER_MIN_OFFSET in store and float(store[CONF_INVERTER_MIN_OFFSET]) <= 0.5:
                store[CONF_INVERTER_MIN_OFFSET] = 1.5
            if CONF_MAX_OVERSHOOT in store and float(store[CONF_MAX_OVERSHOOT]) == 1.0:
                store[CONF_MAX_OVERSHOOT] = 1.2
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=2
        )
        _LOGGER.info(
            "Migration de l'entrée %s vers v2 appliquée (offset_min=1.5 °C, max_overshoot=1.2 °C)",
            config_entry.title,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
