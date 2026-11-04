"""Entité climate virtuelle HA Thermostat Split : modulation inverter et apprentissage anti-yoyo."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from statistics import mean
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .compressor import CompressorTracker, get_tracker, release_tracker
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
from .logic import (
    HOLD,
    START,
    STOP,
    AntiYoyoLearner,
    Decision,
    InverterParams,
    Params,
    compute_inverter_setpoint,
    decide,
)

_LOGGER = logging.getLogger(__name__)

CONTROL_INTERVAL = timedelta(seconds=30)
ADOPT_GRACE = timedelta(seconds=90)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([HAThermostatSplit(hass, entry)])


class HAThermostatSplit(ClimateEntity, RestoreEntity):
    """Thermostat virtuel : moyenne de capteurs, modulation inverter et apprentissage anti-yoyo."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_target_temperature_step = 0.5
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        cfg = {**DEFAULTS, **entry.data, **entry.options}
        self._split: str = cfg[CONF_SPLIT]
        raw_sensors = cfg.get(CONF_SENSORS, [])
        if isinstance(raw_sensors, str):
            self._sensors = [raw_sensors]
        else:
            self._sensors = [str(s) for s in raw_sensors]
        self._power: str = cfg[CONF_POWER]
        self._threshold = float(cfg[CONF_POWER_THRESHOLD])
        self._params = Params(
            cold_tolerance=float(cfg[CONF_COLD_TOL]),
            hot_tolerance=float(cfg[CONF_HOT_TOL]),
            min_on_s=float(cfg[CONF_MIN_ON]) * 60,
            min_off_s=float(cfg[CONF_MIN_OFF]) * 60,
            max_overshoot=float(cfg[CONF_MAX_OVERSHOOT]),
            max_undershoot=float(cfg[CONF_MAX_UNDERSHOOT]),
        )
        self._inverter_params = InverterParams(
            enabled=bool(cfg.get(CONF_INVERTER_MODE, True)),
            min_offset=float(cfg.get(CONF_INVERTER_MIN_OFFSET, 0.5)),
            max_offset=float(cfg.get(CONF_INVERTER_MAX_OFFSET, 3.0)),
            p_band=float(cfg.get(CONF_INVERTER_P_BAND, 1.5)),
            step=0.5,
        )
        self._learning_enabled = bool(cfg.get(CONF_LEARNING_ENABLED, True))
        self._anti_spam = timedelta(seconds=float(cfg.get(CONF_ANTI_SPAM, 90.0)))
        self._split_setpoint = float(cfg[CONF_SPLIT_SETPOINT])
        self._sensor_max_age = float(cfg[CONF_SENSOR_MAX_AGE])
        self._failsafe_s = float(cfg[CONF_FAILSAFE]) * 60
        self._resync = timedelta(minutes=float(cfg[CONF_RESYNC]))

        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title
        )
        self._attr_min_temp = float(cfg[CONF_MIN_TEMP])
        self._attr_max_temp = float(cfg[CONF_MAX_TEMP])

        self._target = float(cfg[CONF_TARGET_DEFAULT])
        self._hvac_mode = HVACMode.OFF
        self._demand = False
        self._temp: float | None = None
        self._used: list[str] = []
        self._ignored: dict[str, str] = {}
        self._reason = "initialisation"
        self._invalid_since: datetime | None = None
        self._last_command: datetime | None = None
        self._last_setpoint_sent: float | None = None
        self._tracker: CompressorTracker | None = None
        self._lock = asyncio.Lock()
        self._control_task: asyncio.Task[None] | None = None

        # Inverter et anti-yoyo
        self._learner = AntiYoyoLearner()
        self._store: Store | None = None
        self._inverter_mode_desc = "inactif"
        self._current_offset = 0.0
        self._slope = 0.0
        self._anticip_offset = 0.0

    # ------------------------------------------------------------------ propriétés
    @property
    def current_temperature(self) -> float | None:
        return self._temp

    @property
    def target_temperature(self) -> float:
        return self._target

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if self._demand else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        split_temp = self._get_split_current_temp()
        bias = (
            round(split_temp - self._temp, 2)
            if split_temp is not None and self._temp is not None
            else None
        )
        tr = self._tracker
        age = tr.age_s(now) if tr else None
        return {
            "split": self._split,
            "capteurs_utilises": self._used,
            "capteurs_ignores": self._ignored,
            "temperature_split": split_temp,
            "biais_capteur_split": bias,
            "consigne_envoyee_au_split": self._last_setpoint_sent,
            "mode_inverter_actif": self._inverter_params.enabled,
            "statut_inverter": self._inverter_mode_desc,
            "offset_inverter": self._current_offset,
            "vitesse_chauffe_deg_h": self._slope,
            "inertie_anticipee_min": round(self._learner.anticipation_h * 60, 1),
            "offset_anticipation_deg": self._anticip_offset,
            "biais_appris": self._learner.learned_bias,
            "dernier_overshoot_deg": self._learner.last_overshoot,
            "cycles_appris": self._learner.cycle_count,
            "anti_spam_secondes": self._anti_spam.total_seconds(),
            "compresseur_actif": tr.is_on if tr else None,
            "compresseur_depuis_min": round(age / 60, 1) if age is not None else None,
            "cycles_aujourdhui": tr.cycles_today() if tr else None,
            "cycles_courts_aujourdhui": tr.short_cycles_today() if tr else None,
            "dernier_cycle_min": tr.last_cycle_minutes() if tr else None,
            "cycle_min_cible_min": self._params.min_on_s / 60,
            "pause_min_cible_min": self._params.min_off_s / 60,
            "decision": self._reason,
        }

    # ------------------------------------------------------------------ cycle de vie
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old is not None:
            if old.state in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(old.state)
            try:
                self._target = float(old.attributes.get(ATTR_TEMPERATURE, self._target))
            except (TypeError, ValueError):
                pass

        if self._learning_enabled:
            self._store = Store(self.hass, 1, f"{DOMAIN}_learning_{self._entry.entry_id}")
            stored_data = await self._store.async_load()
            if stored_data:
                self._learner.load_data(stored_data)
                _LOGGER.debug(
                    "%s : données anti-yoyo chargées (anticipation=%.2f h, biais=%.2f °C)",
                    self.entity_id,
                    self._learner.anticipation_h,
                    self._learner.learned_bias,
                )

        self._tracker = get_tracker(self.hass, self._power, self._threshold)
        self.async_on_remove(self._tracker.register_listener(self._schedule_control))

        # reprise de l'état réel du split
        split = self.hass.states.get(self._split)
        if split is not None and split.state == HVACMode.HEAT:
            self._demand = True
            self._tracker.set_active(self._entry.entry_id, True)
            if self._learning_enabled and self._temp is not None:
                self._learner.start_cycle(self._target, self._temp)

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._sensors + [self._split], self._on_input_change
            )
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, CONTROL_INTERVAL)
        )
        self._schedule_control()

    async def async_will_remove_from_hass(self) -> None:
        if self._tracker is not None:
            self._tracker.set_active(self._entry.entry_id, False)
            release_tracker(self.hass, self._tracker)
            self._tracker = None
        if self._store is not None and self._learning_enabled:
            await self._store.async_save(self._learner.to_dict())

    # ------------------------------------------------------------------ déclencheurs
    @callback
    def _on_input_change(self, event: Event) -> None:
        self._schedule_control()

    @callback
    def _schedule_control(self) -> None:
        if self._control_task is not None and not self._control_task.done():
            return
        self._control_task = self.hass.async_create_task(self._async_control())

    async def _async_tick(self, now: datetime) -> None:
        await self._async_control()

    # ------------------------------------------------------------------ commandes utilisateur
    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._target = max(self._attr_min_temp, min(self._attr_max_temp, float(temp)))
        self.async_write_ha_state()
        await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        async with self._lock:
            if hvac_mode == HVACMode.OFF:
                self._hvac_mode = HVACMode.OFF
                if self._demand:
                    await self._stop(dt_util.utcnow(), forced=True)
            elif hvac_mode == HVACMode.HEAT:
                self._hvac_mode = HVACMode.HEAT
            self.async_write_ha_state()
        await self._async_control()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    # ------------------------------------------------------------------ température
    def _compute_temperature(self, now: datetime) -> float | None:
        values: list[float] = []
        used: list[str] = []
        ignored: dict[str, str] = {}
        for entity_id in self._sensors:
            st = self.hass.states.get(entity_id)
            if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                ignored[entity_id] = "indisponible"
                continue
            try:
                val = float(st.state)
            except ValueError:
                ignored[entity_id] = "valeur invalide"
                continue
            ref = getattr(st, "last_reported", None) or st.last_updated
            age_min = (now - ref).total_seconds() / 60
            if age_min > self._sensor_max_age:
                ignored[entity_id] = f"trop ancien ({age_min:.0f} min)"
                continue
            values.append(val)
            used.append(entity_id)
        self._used = used
        self._ignored = ignored
        return round(mean(values), 2) if values else None

    def _get_split_current_temp(self) -> float | None:
        split = self.hass.states.get(self._split)
        if split is not None:
            try:
                return float(split.attributes.get("current_temperature"))
            except (TypeError, ValueError):
                pass
        return None

    # ------------------------------------------------------------------ boucle de régulation
    async def _async_control(self) -> None:
        async with self._lock:
            now = dt_util.utcnow()
            self._temp = self._compute_temperature(now)
            self._adopt_split_state(now)

            if self._temp is not None:
                self._learner.add_sample(now.timestamp(), self._temp, self._params.hot_tolerance)
                self._slope = self._learner.compute_slope()
                self._anticip_offset = (
                    self._learner.get_anticipation_offset(self._slope)
                    if self._learning_enabled
                    else 0.0
                )

            if self._hvac_mode == HVACMode.OFF:
                self._reason = "régulation arrêtée"
                self.async_write_ha_state()
                return

            if self._temp is None:
                await self._handle_no_sensor(now)
                self.async_write_ha_state()
                return
            self._invalid_since = None

            tracker = self._tracker
            decision = decide(
                heating=self._demand,
                temp=self._temp,
                target=self._target,
                params=self._params,
                compressor_on=tracker.is_on,
                compressor_age_s=tracker.age_s(now),
                others_active=tracker.others_active(self._entry.entry_id),
            )

            # Application du délai anti-spam
            if decision.action != HOLD and self._last_command is not None:
                if now - self._last_command < self._anti_spam:
                    decision = Decision(HOLD, f"temporisation anti-spam (encore {round((self._anti_spam - (now - self._last_command)).total_seconds())} s)")

            self._reason = decision.reason
            if decision.action == START:
                await self._start(now)
            elif decision.action == STOP:
                await self._stop(now)
            else:
                await self._reassert(now)
            self.async_write_ha_state()

    async def _handle_no_sensor(self, now: datetime) -> None:
        if self._invalid_since is None:
            self._invalid_since = now
        elapsed = (now - self._invalid_since).total_seconds()
        if self._demand and elapsed >= self._failsafe_s:
            self._reason = "aucun capteur valide : arrêt de sécurité"
            await self._stop(now, forced=True)
        else:
            self._reason = "aucun capteur de température valide"

    @callback
    def _adopt_split_state(self, now: datetime) -> None:
        """Si quelqu'un agit directement sur le split, on s'aligne (hors période de grâce)."""
        if self._last_command is not None and now - self._last_command < ADOPT_GRACE:
            return
        split = self.hass.states.get(self._split)
        if split is None or split.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        is_heat = split.state == HVACMode.HEAT
        if is_heat != self._demand:
            _LOGGER.debug("%s : split passé en %s hors régulation, alignement", self.entity_id, split.state)
            self._demand = is_heat
            # Si le split physique a été éteint manuellement, aligner le thermostat virtuel sur OFF
            # pour éviter qu'il ne se rallume tout seul au prochain cycle
            if not is_heat and self._hvac_mode == HVACMode.HEAT:
                self._hvac_mode = HVACMode.OFF
                _LOGGER.info("%s : arrêt manuel du split détecté, thermostat virtuel basculé sur OFF", self.entity_id)
            if self._tracker is not None:
                self._tracker.set_active(self._entry.entry_id, is_heat)

    # ------------------------------------------------------------------ commandes vers le split
    async def _call(self, service: str, data: dict[str, Any]) -> bool:
        try:
            await self.hass.services.async_call(
                "climate", service, {"entity_id": self._split, **data}, blocking=True
            )
            return True
        except HomeAssistantError as err:
            _LOGGER.error("%s : échec de climate.%s sur %s : %s", self.entity_id, service, self._split, err)
            return False

    def _split_setpoint_to_send(self) -> float:
        split_temp = self._get_split_current_temp()
        split = self.hass.states.get(self._split)
        max_temp = 30.0
        min_temp = 16.0
        step = 0.5
        if split is not None:
            try:
                max_temp = float(split.attributes.get("max_temp", 30.0))
                min_temp = float(split.attributes.get("min_temp", 16.0))
                step = float(split.attributes.get("target_temperature_step", 0.5))
            except (TypeError, ValueError):
                pass

        if self._temp is None:
            return min(max_temp, max(min_temp, self._split_setpoint))

        inverter_params = InverterParams(
            enabled=self._inverter_params.enabled,
            min_offset=self._inverter_params.min_offset,
            max_offset=self._inverter_params.max_offset,
            p_band=self._inverter_params.p_band,
            step=step,
        )

        setpoint, offset, mode_desc = compute_inverter_setpoint(
            room_temp=self._temp,
            target_temp=self._target,
            split_temp=split_temp,
            fallback_setpoint=self._split_setpoint,
            params=inverter_params,
            anticipation_offset=self._anticip_offset,
            min_temp=min_temp,
            max_temp=max_temp,
        )
        self._current_offset = offset
        self._inverter_mode_desc = mode_desc

        if (
            self._learning_enabled
            and split_temp is not None
            and abs(self._target - self._temp) <= 0.3
            and self._tracker
            and self._tracker.is_on
        ):
            self._learner.update_steady_bias(split_temp, self._temp)

        return setpoint

    async def _send_heat(self) -> bool:
        split = self.hass.states.get(self._split)
        setpoint = self._split_setpoint_to_send()

        # Si le split n'est pas encore en mode chauffe, on utilise un SEUL appel combiné
        # (consigne + hvac_mode: heat) supporté par Home Assistant pour éviter tout double envoi
        if split is None or split.state != HVACMode.HEAT:
            ok = await self._call(
                "set_temperature",
                {ATTR_TEMPERATURE: setpoint, "hvac_mode": HVACMode.HEAT},
            )
            if not ok:
                # Repli uniquement si l'intégration cible refuse hvac_mode dans set_temperature
                if not await self._call("set_hvac_mode", {"hvac_mode": "heat"}):
                    return False
                await asyncio.sleep(1.0)
                ok = await self._call("set_temperature", {ATTR_TEMPERATURE: setpoint})
            if ok:
                self._last_setpoint_sent = setpoint
            return ok

        # Si le split est déjà en chauffe et déjà à cette consigne, aucun appel inutile !
        if self._last_setpoint_sent is not None and abs(self._last_setpoint_sent - setpoint) < 0.2:
            return True

        if await self._call("set_temperature", {ATTR_TEMPERATURE: setpoint}):
            self._last_setpoint_sent = setpoint
            return True
        return False

    async def _start(self, now: datetime) -> None:
        if await self._send_heat():
            self._demand = True
            self._last_command = now
            self._tracker.set_active(self._entry.entry_id, True)
            if self._learning_enabled and self._temp is not None:
                self._learner.start_cycle(self._target, self._temp)
            _LOGGER.debug("%s : démarrage (%s)", self.entity_id, self._reason)

    async def _stop(self, now: datetime, forced: bool = False) -> None:
        split = self.hass.states.get(self._split)
        modes = (split.attributes.get("hvac_modes") if split else None) or []
        if "off" in modes or not modes:
            ok = await self._call("set_hvac_mode", {"hvac_mode": "off"})
        else:
            ok = await self._call("turn_off", {})
        if ok:
            self._demand = False
            self._last_command = now
            self._tracker.set_active(self._entry.entry_id, False)
            if self._learning_enabled:
                self._learner.stop_heating(now.timestamp())
                if self._store is not None:
                    self.hass.async_create_task(self._store.async_save(self._learner.to_dict()))
            _LOGGER.debug("%s : arrêt (%s)", self.entity_id, self._reason)

    async def _reassert(self, now: datetime) -> None:
        """Ajuste ou réaffirme la consigne modulante vers le split en respectant l'anti-spam."""
        if not self._demand:
            return
        if self._last_command is not None and now - self._last_command < self._anti_spam:
            return

        split = self.hass.states.get(self._split)
        if split is None:
            return
        try:
            current = float(split.attributes.get(ATTR_TEMPERATURE))
        except (TypeError, ValueError):
            return

        wanted = self._split_setpoint_to_send()
        # On n'appelle le service QUE si la consigne demandée a réellement changé d'au moins 0.4°C
        # Cela garantit aucun appel inutile quand le split est déjà à la bonne température
        if abs(current - wanted) >= 0.4:
            if await self._call("set_temperature", {ATTR_TEMPERATURE: wanted}):
                self._last_setpoint_sent = wanted
                self._last_command = now
                _LOGGER.debug(
                    "%s : consigne modulée ajustée à %.1f °C (%s)",
                    self.entity_id,
                    wanted,
                    self._inverter_mode_desc,
                )
