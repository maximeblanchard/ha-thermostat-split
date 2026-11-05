"""Commandes WebSocket pour la carte Lovelace de planification hebdomadaire."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


def _get_entity(hass: HomeAssistant, entity_id: str):
    entities: dict[str, Any] = hass.data.get(DOMAIN, {}).get("entities", {})
    for ent in entities.values():
        if getattr(ent, "entity_id", None) == entity_id:
            return ent
    return None


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Enregistre une seule fois les commandes WebSocket de l'intégration."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("ws_registered"):
        return
    websocket_api.async_register_command(hass, ws_get_schedule)
    websocket_api.async_register_command(hass, ws_set_schedule)
    websocket_api.async_register_command(hass, ws_clear_override)
    domain_data["ws_registered"] = True


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/get_schedule",
        vol.Required("entity_id"): str,
    }
)
@websocket_api.async_response
async def ws_get_schedule(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Retourne le planning hebdomadaire complet d'une entité."""
    entity = _get_entity(hass, msg["entity_id"])
    if entity is None:
        connection.send_error(
            msg["id"], "not_found", f"Entité {msg['entity_id']} introuvable"
        )
        return
    connection.send_result(msg["id"], entity.get_schedule_payload())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_schedule",
        vol.Required("entity_id"): str,
        vol.Optional("enabled"): bool,
        vol.Optional("presets"): dict,
        vol.Optional("days"): dict,
        vol.Optional("apply_now", default=True): bool,
    }
)
@websocket_api.async_response
async def ws_set_schedule(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Met à jour et sauvegarde le planning hebdomadaire et/ou les températures des presets."""
    entity = _get_entity(hass, msg["entity_id"])
    if entity is None:
        connection.send_error(
            msg["id"], "not_found", f"Entité {msg['entity_id']} introuvable"
        )
        return

    await entity.async_update_schedule(
        enabled=msg.get("enabled"),
        presets=msg.get("presets"),
        days=msg.get("days"),
        apply_now=bool(msg.get("apply_now", True)),
    )
    connection.send_result(msg["id"], entity.get_schedule_payload())


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/clear_override",
        vol.Required("entity_id"): str,
    }
)
@websocket_api.async_response
async def ws_clear_override(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Annule immédiatement une dérogation manuelle temporaire et reprend le planning."""
    entity = _get_entity(hass, msg["entity_id"])
    if entity is None:
        connection.send_error(
            msg["id"], "not_found", f"Entité {msg['entity_id']} introuvable"
        )
        return

    await entity.async_clear_schedule_override()
    connection.send_result(msg["id"], entity.get_schedule_payload())
