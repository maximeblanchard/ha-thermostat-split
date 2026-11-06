"""HA Thermostat Split : thermostat virtuel par pièce pour splits inverter."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_INVERTER_MAX_OFFSET,
    CONF_INVERTER_MIN_OFFSET,
    CONF_MAX_OVERSHOOT,
    DOMAIN,
    VERSION,
)
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE]

STATIC_URL_BASE = f"/{DOMAIN}_static"
CARD_FILENAME = "ha-thermostat-split-card.js"
CARD_URL = f"{STATIC_URL_BASE}/{CARD_FILENAME}?v={VERSION}"


def _get_lovelace_data(hass: HomeAssistant):
    """Récupère l'objet Lovelace de façon compatible HA 2024.x, 2025.2+ et 2026.x (HassKey)."""
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA  # type: ignore[attr-defined]

        data = hass.data.get(LOVELACE_DATA)
        if data is not None:
            return data
    except ImportError:
        pass
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA as LOVELACE_DATA_ALT  # type: ignore[attr-defined]

        data = hass.data.get(LOVELACE_DATA_ALT)
        if data is not None:
            return data
    except ImportError:
        pass
    direct = hass.data.get("lovelace")
    if direct is not None:
        return direct
    for key, val in hass.data.items():
        if getattr(key, "value", None) == "lovelace":
            return val
    return None


async def _async_ensure_lovelace_resource(hass: HomeAssistant) -> bool:
    """Enregistre ou met à jour la carte dans les ressources Lovelace (mode storage)."""
    try:
        lovelace = _get_lovelace_data(hass)
        if lovelace is None:
            return False
        resources = (
            lovelace.get("resources")
            if isinstance(lovelace, dict)
            else getattr(lovelace, "resources", None)
        )
        if resources is None or not hasattr(resources, "async_items"):
            return False
        if (
            hasattr(resources, "loaded")
            and not resources.loaded
            and hasattr(resources, "async_load")
        ):
            await resources.async_load()
        prefix = f"{STATIC_URL_BASE}/{CARD_FILENAME}"
        for item in resources.async_items():
            url = str(item.get("url", ""))
            if url.startswith(prefix):
                if url != CARD_URL and hasattr(resources, "async_update_item"):
                    await resources.async_update_item(
                        item["id"], {"res_type": "module", "url": CARD_URL}
                    )
                    _LOGGER.info("Ressource Lovelace mise à jour : %s", CARD_URL)
                return True
        if hasattr(resources, "async_create_item"):
            await resources.async_create_item({"res_type": "module", "url": CARD_URL})
            _LOGGER.info("Ressource Lovelace enregistrée automatiquement : %s", CARD_URL)
            return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Impossible d'enregistrer automatiquement lovelace.resources : %s", err)
    return False


async def _async_register_frontend_and_ws(hass: HomeAssistant) -> None:
    """Enregistre de façon idempotente l'API WebSocket et la carte Lovelace embarquée."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    async_register_websocket(hass)

    if domain_data.get("frontend_registered"):
        return

    frontend_dir = Path(__file__).parent / "frontend"
    if getattr(hass, "http", None) is not None and frontend_dir.is_dir():
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL_BASE, str(frontend_dir), cache_headers=False)]
        )
        add_extra_js_url(hass, CARD_URL)

        registered = await _async_ensure_lovelace_resource(hass)
        if not registered and not hass.is_running:

            async def _on_started(_event: Event) -> None:
                await _async_ensure_lovelace_resource(hass)

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

        domain_data["frontend_registered"] = True


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialisation globale du domaine HA Thermostat Split."""
    await _async_register_frontend_and_ws(hass)
    return True


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
    if config_entry.version < 3:
        new_data = {**config_entry.data}
        new_options = {**config_entry.options}
        for store in (new_data, new_options):
            if store.get(CONF_INVERTER_MIN_OFFSET) == 1.5:
                store[CONF_INVERTER_MIN_OFFSET] = 0.5
            if store.get(CONF_INVERTER_MAX_OFFSET) == 3.0:
                store[CONF_INVERTER_MAX_OFFSET] = 2.0
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=3
        )
        _LOGGER.info(
            "Migration de l'entrée %s vers v3 appliquée (offset_min=0.5 °C, offset_max=2.0 °C)",
            config_entry.title,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_register_frontend_and_ws(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
