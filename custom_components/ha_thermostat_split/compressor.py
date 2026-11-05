"""Suivi de l'état du compresseur à partir de la puissance électrique totale de la PAC."""
from __future__ import annotations

from collections import deque
from datetime import datetime
import logging
from typing import Callable

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class CompressorTracker:
    """État partagé du compresseur (un seul groupe extérieur pour plusieurs splits)."""

    def __init__(self, hass: HomeAssistant, power_entity: str, threshold_w: float) -> None:
        self.hass = hass
        self.power_entity = power_entity
        self.threshold_w = threshold_w
        self.is_on = False
        self.since: datetime | None = None
        self.refs = 0
        self._cycle_start: datetime | None = None
        self._history: deque[tuple[datetime, datetime]] = deque(maxlen=1000)
        self._listeners: list[Callable[[], None]] = []
        self._active_rooms: set[str] = set()
        self._unsub: Callable[[], None] | None = None

    # ------------------------------------------------------------------ cycle de vie
    @callback
    def async_start(self) -> None:
        w = self._to_watts(self.hass.states.get(self.power_entity))
        if w is not None:
            self.is_on = w > self.threshold_w
            now = dt_util.utcnow()
            self.since = now
            if self.is_on:
                self._cycle_start = now
        self._unsub = async_track_state_change_event(
            self.hass, [self.power_entity], self._handle_power
        )

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    # ------------------------------------------------------------------ lecture
    @staticmethod
    def _to_watts(state: State | None) -> float | None:
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if state.attributes.get("unit_of_measurement") == "kW":
            value *= 1000.0
        return value

    @callback
    def _handle_power(self, event: Event) -> None:
        watts = self._to_watts(event.data.get("new_state"))
        if watts is None:
            return  # on garde l'état précédent
        on = watts > self.threshold_w
        if on == self.is_on:
            return
        now = dt_util.utcnow()
        if on:
            self._cycle_start = now
        elif self._cycle_start is not None:
            self._history.append((self._cycle_start, now))
            self._cycle_start = None
        self.is_on = on
        self.since = now
        _LOGGER.debug("Compresseur %s (%.0f W)", "ON" if on else "OFF", watts)
        self._notify()

    @callback
    def update_threshold(self, threshold_w: float) -> None:
        """Met à jour dynamiquement le seuil de détection compresseur et réévalue l'état."""
        if abs(self.threshold_w - threshold_w) < 0.01:
            return
        self.threshold_w = threshold_w
        watts = self._to_watts(self.hass.states.get(self.power_entity))
        if watts is None:
            return
        on = watts > self.threshold_w
        if on != self.is_on:
            now = dt_util.utcnow()
            if on:
                self._cycle_start = now
            elif self._cycle_start is not None:
                self._history.append((self._cycle_start, now))
                self._cycle_start = None
            self.is_on = on
            self.since = now
            self._notify()

    # ------------------------------------------------------------------ pièces actives
    def set_active(self, room_id: str, active: bool) -> None:
        if active:
            changed = room_id not in self._active_rooms
            self._active_rooms.add(room_id)
        else:
            changed = room_id in self._active_rooms
            self._active_rooms.discard(room_id)
        if changed:
            self._notify()

    def others_active(self, room_id: str) -> bool:
        return bool(self._active_rooms - {room_id})

    # ------------------------------------------------------------------ infos
    def age_s(self, now: datetime) -> float | None:
        return None if self.since is None else (now - self.since).total_seconds()

    def _today(self) -> list[tuple[datetime, datetime]]:
        start = dt_util.start_of_local_day()
        return [c for c in self._history if c[0] >= start]

    def cycles_today(self) -> int:
        return len(self._today()) + (1 if self._cycle_start and self._cycle_start >= dt_util.start_of_local_day() else 0)

    def short_cycles_today(self, limit_min: float = 15.0) -> int:
        return sum(1 for s, e in self._today() if (e - s).total_seconds() < limit_min * 60)

    def last_cycle_minutes(self) -> float | None:
        if not self._history:
            return None
        s, e = self._history[-1]
        return round((e - s).total_seconds() / 60, 1)

    # ------------------------------------------------------------------ écouteurs
    def register_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return _remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Erreur dans un écouteur du compresseur")


def get_tracker(hass: HomeAssistant, power_entity: str, threshold_w: float) -> CompressorTracker:
    """Retourne (en le créant si besoin) le suivi partagé de ce compteur de puissance."""
    registry = hass.data.setdefault(DOMAIN, {}).setdefault("trackers", {})
    tracker = registry.get(power_entity)
    if tracker is None:
        tracker = CompressorTracker(hass, power_entity, float(threshold_w))
        registry[power_entity] = tracker
        tracker.async_start()
    else:
        tracker.update_threshold(float(threshold_w))
    tracker.refs += 1
    return tracker


def release_tracker(hass: HomeAssistant, tracker: CompressorTracker) -> None:
    tracker.refs -= 1
    if tracker.refs <= 0:
        tracker.async_stop()
        registry = hass.data.get(DOMAIN, {}).get("trackers", {})
        registry.pop(tracker.power_entity, None)
