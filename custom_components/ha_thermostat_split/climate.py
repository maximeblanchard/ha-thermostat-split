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
    CONF_MODULATION_INTERVAL,
    CONF_OUTDOOR_TEMP,
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
from .schedule import (
    ALL_PRESETS,
    PRESET_COMFORT,
    PRESET_NONE,
    PRESET_SCHEDULE,
    TEMP_PRESETS,
    ScheduleEvaluation,
    WeeklySchedule,
)

_LOGGER = logging.getLogger(__name__)

CONTROL_INTERVAL = timedelta(seconds=30)
ADOPT_GRACE = timedelta(seconds=180)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([HAThermostatSplit(hass, entry)])


class HAThermostatSplit(ClimateEntity, RestoreEntity):
    """Thermostat virtuel : moyenne de capteurs, modulation inverter, planning hebdo et anti-yoyo."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "thermostat"
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_preset_modes = ALL_PRESETS
    _attr_target_temperature_step = 0.5
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
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
            max_offset=float(cfg.get(CONF_INVERTER_MAX_OFFSET, 2.0)),
            p_band=float(cfg.get(CONF_INVERTER_P_BAND, 1.5)),
            step=0.5,
        )
        self._outdoor_sensor: str | None = cfg.get(CONF_OUTDOOR_TEMP)
        self._outdoor_temp: float | None = None
        self._learning_enabled = bool(cfg.get(CONF_LEARNING_ENABLED, True))
        self._anti_spam = timedelta(seconds=float(cfg.get(CONF_ANTI_SPAM, 90.0)))
        self._modulation_interval = timedelta(
            seconds=float(cfg.get(CONF_MODULATION_INTERVAL, 300.0))
        )
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
        self._cycle_started_at: datetime | None = None
        self._split_confirmed_heat = False
        self._user_target_changed = False
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

        # Planning hebdomadaire et modes préréglés
        self._schedule = WeeklySchedule(
            enabled=False,
            min_temp=self._attr_min_temp,
            max_temp=self._attr_max_temp,
        )
        self._schedule_store: Store | None = None
        self._preset_mode: str = PRESET_NONE
        self._override_block_sig: str | None = None
        self._last_applied_block_sig: str | None = None
        self._last_schedule_eval: ScheduleEvaluation | None = None
        self._schedule_version: int = 1

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
    def preset_mode(self) -> str:
        return self._preset_mode

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
        ev = self._last_schedule_eval or self._schedule.evaluate(dt_util.as_local(now))
        return {
            "split": self._split,
            "capteurs_utilises": self._used,
            "capteurs_ignores": self._ignored,
            "temperature_split": split_temp,
            "temperature_split_filtree": self._learner.filtered_split_temp,
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
            "temperature_exterieure": self._outdoor_temp,
            "constante_temps_tau_h": self._learner.tau_h,
            "deperdition_deg_h_pour_10k": self._learner.loss_rate_per_10k,
            "efficacite_thermique_label": self._learner.thermal_efficiency_label,
            "puissance_maintien_estimee_w": self._learner.estimated_heat_need_w(self._target),
            "anti_spam_secondes": self._anti_spam.total_seconds(),
            "intervalle_modulation_secondes": self._modulation_interval.total_seconds(),
            "compresseur_actif": tr.is_on if tr else None,
            "compresseur_depuis_min": round(age / 60, 1) if age is not None else None,
            "cycles_aujourdhui": tr.cycles_today() if tr else None,
            "cycles_courts_aujourdhui": tr.short_cycles_today() if tr else None,
            "dernier_cycle_min": tr.last_cycle_minutes() if tr else None,
            "cycle_min_cible_min": self._params.min_on_s / 60,
            "pause_min_cible_min": self._params.min_off_s / 60,
            "planning_actif": self._schedule.enabled and self._preset_mode == PRESET_SCHEDULE,
            "planning_version": self._schedule_version,
            "creneau_actuel": ev.slot_mode if self._schedule.enabled else None,
            "prochain_changement": ev.next_change_label if self._schedule.enabled else None,
            "derogation_planning": self._override_block_sig is not None,
            "presets_temperatures": dict(self._schedule.presets),
            "decision": self._reason,
        }

    # ------------------------------------------------------------------ cycle de vie
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.data.setdefault(DOMAIN, {}).setdefault("entities", {})[
            self._entry.entry_id
        ] = self

        old = await self.async_get_last_state()
        restored_preset: str | None = None
        if old is not None:
            if old.state in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(old.state)
            try:
                restored_t = float(old.attributes.get(ATTR_TEMPERATURE, self._target))
                self._target = max(self._attr_min_temp, min(self._attr_max_temp, restored_t))
            except (TypeError, ValueError):
                pass
            old_preset = old.attributes.get("preset_mode")
            if isinstance(old_preset, str) and old_preset in ALL_PRESETS:
                restored_preset = old_preset

        self._schedule_store = Store(
            self.hass, 1, f"{DOMAIN}_schedule_{self._entry.entry_id}"
        )
        sched_data = await self._schedule_store.async_load()
        if sched_data:
            self._schedule.load_data(sched_data)
        self._schedule.update_bounds(self._attr_min_temp, self._attr_max_temp)

        if restored_preset is not None:
            if restored_preset == PRESET_SCHEDULE and not self._schedule.enabled:
                self._preset_mode = PRESET_NONE
            else:
                self._preset_mode = restored_preset
        elif self._schedule.enabled:
            self._preset_mode = PRESET_SCHEDULE

        if (
            old is not None
            and old.attributes.get("derogation_planning") is True
            and self._preset_mode == PRESET_SCHEDULE
            and self._schedule.enabled
        ):
            self._override_block_sig = self._schedule.evaluate(dt_util.now()).block_signature

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
        now = dt_util.utcnow()
        self._temp = self._compute_temperature(now)
        split = self.hass.states.get(self._split)
        if split is not None and split.state == HVACMode.HEAT:
            self._demand = True
            self._split_confirmed_heat = True
            self._cycle_started_at = now - timedelta(minutes=10)
            self._tracker.set_active(self._entry.entry_id, True)
            if self._learning_enabled and self._temp is not None:
                self._learner.start_cycle(self._target, self._temp)

        watch_entities = list(self._sensors) + [self._split]
        if self._outdoor_sensor:
            watch_entities.append(self._outdoor_sensor)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, watch_entities, self._on_input_change
            )
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, CONTROL_INTERVAL)
        )
        self._schedule_control()

    async def async_will_remove_from_hass(self) -> None:
        self.hass.data.get(DOMAIN, {}).get("entities", {}).pop(self._entry.entry_id, None)
        if self._tracker is not None:
            self._tracker.set_active(self._entry.entry_id, False)
            release_tracker(self.hass, self._tracker)
            self._tracker = None
        if self._store is not None and self._learning_enabled:
            await self._store.async_save(self._learner.to_dict())
        if self._schedule_store is not None:
            await self._schedule_store.async_save(self._schedule.to_dict())

    @callback
    def _schedule_learner_save(self) -> None:
        """Planifie une sauvegarde différée (30 s) des données apprises sans saturer le disque."""
        if self._learning_enabled and self._store is not None:
            self._store.async_delay_save(self._learner.to_dict, 30.0)

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
        async with self._lock:
            new_target = max(self._attr_min_temp, min(self._attr_max_temp, float(temp)))
            if self._preset_mode == PRESET_SCHEDULE and self._schedule.enabled:
                ev = self._schedule.evaluate(dt_util.now())
                self._override_block_sig = ev.block_signature
                self._hvac_mode = HVACMode.HEAT
            elif self._preset_mode in TEMP_PRESETS:
                if abs(new_target - self._schedule.presets[self._preset_mode]) >= 0.05:
                    self._preset_mode = PRESET_NONE
            if abs(new_target - self._target) >= 0.05:
                self._user_target_changed = True
            self._target = new_target
            self.async_write_ha_state()
        await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        async with self._lock:
            now = dt_util.utcnow()
            ev = self._schedule.evaluate(dt_util.as_local(now))
            if hvac_mode == HVACMode.OFF:
                if (
                    self._preset_mode == PRESET_SCHEDULE
                    and self._schedule.enabled
                    and ev.hvac_mode != "off"
                ):
                    self._override_block_sig = ev.block_signature
                self._hvac_mode = HVACMode.OFF
                if self._demand:
                    await self._stop(now, forced=True)
            elif hvac_mode == HVACMode.HEAT:
                self._hvac_mode = HVACMode.HEAT
                if self._preset_mode == PRESET_SCHEDULE and self._schedule.enabled:
                    if ev.hvac_mode == "off":
                        self._override_block_sig = ev.block_signature
                        comfort_t = self._schedule.presets[PRESET_COMFORT]
                        if abs(self._target - comfort_t) >= 0.05:
                            self._target = comfort_t
                            self._user_target_changed = True
                    else:
                        self._override_block_sig = None
                        if ev.target_temp is not None and abs(self._target - ev.target_temp) >= 0.05:
                            self._target = ev.target_temp
                            self._user_target_changed = True
            self.async_write_ha_state()
        await self._async_control()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in ALL_PRESETS:
            return
        async with self._lock:
            now = dt_util.utcnow()
            self._preset_mode = preset_mode
            self._override_block_sig = None
            if preset_mode == PRESET_SCHEDULE:
                if not self._schedule.enabled:
                    self._schedule.enabled = True
                    self._schedule_version += 1
                    if self._schedule_store is not None:
                        await self._schedule_store.async_save(self._schedule.to_dict())
                await self._evaluate_and_apply_schedule_locked(now, force_apply=True)
            elif preset_mode in TEMP_PRESETS:
                new_t = self._schedule.presets[preset_mode]
                if abs(new_t - self._target) >= 0.05:
                    self._user_target_changed = True
                self._target = new_t
                self._hvac_mode = HVACMode.HEAT
            else:
                self._hvac_mode = HVACMode.HEAT
            self.async_write_ha_state()
        await self._async_control()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    # ------------------------------------------------------------------ API planning hebdomadaire
    def get_schedule_payload(self) -> dict[str, Any]:
        """Données complètes du planning envoyées à la carte Lovelace via WebSocket."""
        ev = self._schedule.evaluate(dt_util.now())
        self._last_schedule_eval = ev
        return {
            "entity_id": self.entity_id,
            "name": self._entry.title,
            "enabled": self._schedule.enabled,
            "preset_mode": self._preset_mode,
            "hvac_mode": self._hvac_mode,
            "override_active": self._override_block_sig is not None,
            "current_slot_mode": ev.slot_mode,
            "current_slot_start": ev.slot_start,
            "next_change": ev.next_change_label,
            "next_hvac_mode": ev.next_hvac_mode,
            "next_target_temp": ev.next_target_temp,
            "min_temp": self._attr_min_temp,
            "max_temp": self._attr_max_temp,
            "presets": dict(self._schedule.presets),
            "days": self._schedule.to_dict()["days"],
            "version": self._schedule_version,
            "diagnostics": {
                "outdoor_temp": self._outdoor_temp,
                "anticipation_min": round(self._learner.anticipation_h * 60, 1),
                "learned_bias": self._learner.learned_bias,
                "last_overshoot": self._learner.last_overshoot,
                "cycle_count": self._learner.cycle_count,
                "slope": self._slope,
                "tau_h": self._learner.tau_h,
                "loss_rate_per_10k": self._learner.loss_rate_per_10k,
                "efficiency_label": self._learner.thermal_efficiency_label,
                "heat_need_w": self._learner.estimated_heat_need_w(self._target),
                "inverter_status": self._inverter_mode_desc,
                "offset": self._current_offset,
            },
        }

    async def async_update_schedule(
        self,
        *,
        enabled: bool | None = None,
        presets: dict[str, Any] | None = None,
        days: dict[str, Any] | None = None,
        apply_now: bool = True,
    ) -> None:
        """Met à jour et persiste le planning hebdomadaire depuis la carte Lovelace."""
        async with self._lock:
            if presets is not None:
                self._schedule.update_presets(presets)
                if self._preset_mode in TEMP_PRESETS:
                    new_t = self._schedule.presets[self._preset_mode]
                    if abs(new_t - self._target) >= 0.05:
                        self._target = new_t
                        self._user_target_changed = True
            if days is not None:
                self._schedule.update_days(days)
            if enabled is not None:
                self._schedule.enabled = bool(enabled)
                if self._schedule.enabled:
                    self._preset_mode = PRESET_SCHEDULE
                elif self._preset_mode == PRESET_SCHEDULE:
                    self._preset_mode = PRESET_NONE
                    self._override_block_sig = None

            self._schedule_version += 1
            if self._schedule_store is not None:
                await self._schedule_store.async_save(self._schedule.to_dict())

            if apply_now and self._schedule.enabled and self._preset_mode == PRESET_SCHEDULE:
                await self._evaluate_and_apply_schedule_locked(
                    dt_util.utcnow(), force_apply=True
                )
            self.async_write_ha_state()
        await self._async_control()

    async def async_clear_schedule_override(self) -> None:
        """Annule une dérogation manuelle temporaire et réapplique immédiatement le créneau actif."""
        async with self._lock:
            self._override_block_sig = None
            if self._schedule.enabled:
                self._preset_mode = PRESET_SCHEDULE
                await self._evaluate_and_apply_schedule_locked(
                    dt_util.utcnow(), force_apply=True
                )
            self._schedule_version += 1
            self.async_write_ha_state()
        await self._async_control()

    async def _evaluate_and_apply_schedule_locked(
        self, now: datetime, force_apply: bool = False
    ) -> None:
        """Applique les transitions horaires du planning (appelé sous self._lock)."""
        ev = self._schedule.evaluate(dt_util.as_local(now))
        self._last_schedule_eval = ev
        if not self._schedule.enabled or self._preset_mode != PRESET_SCHEDULE:
            return

        if force_apply:
            self._override_block_sig = None
            self._last_applied_block_sig = None

        if self._override_block_sig is not None:
            if ev.block_signature != self._override_block_sig:
                # Changement de créneau atteint : fin automatique de la dérogation manuelle
                self._override_block_sig = None
            else:
                return

        needs_sync = (
            ev.block_signature != self._last_applied_block_sig
            or (ev.hvac_mode == "off" and self._hvac_mode != HVACMode.OFF)
            or (
                ev.hvac_mode == "heat"
                and (
                    self._hvac_mode != HVACMode.HEAT
                    or (
                        ev.target_temp is not None
                        and abs(self._target - ev.target_temp) >= 0.05
                    )
                )
            )
        )
        if not needs_sync:
            return

        self._last_applied_block_sig = ev.block_signature
        if ev.hvac_mode == "off":
            self._hvac_mode = HVACMode.OFF
            if self._demand:
                await self._stop(now, forced=True)
        elif ev.target_temp is not None:
            if abs(self._target - ev.target_temp) >= 0.05:
                self._target = ev.target_temp
                self._user_target_changed = True
            self._hvac_mode = HVACMode.HEAT

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

    def _compute_outdoor_temperature(self) -> float | None:
        if not self._outdoor_sensor:
            return None
        st = self.hass.states.get(self._outdoor_sensor)
        if st is None or st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return round(float(st.state), 1)
        except (ValueError, TypeError):
            return None

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
            now_ts = now.timestamp()
            self._temp = self._compute_temperature(now)
            self._outdoor_temp = self._compute_outdoor_temperature()
            self._adopt_split_state(now)
            await self._evaluate_and_apply_schedule_locked(now)

            if self._temp is not None:
                if self._learning_enabled:
                    if self._demand:
                        if self._learner.add_sample(now_ts, self._temp, self._params.hot_tolerance):
                            self._schedule_learner_save()
                    else:
                        if self._learner.add_cooling_sample(now_ts, self._temp, self._outdoor_temp):
                            self._schedule_learner_save()

                self._slope = self._learner.compute_slope()
                self._anticip_offset = (
                    self._learner.get_anticipation_offset(self._slope)
                    if self._learning_enabled
                    else 0.0
                )
                if self._demand:
                    cycle_elapsed_s = (
                        max(0.0, (now - self._cycle_started_at).total_seconds())
                        if self._cycle_started_at is not None
                        else None
                    )
                    self._learner.update_split_temp(
                        self._get_split_current_temp(),
                        self._temp,
                        cycle_elapsed_s=cycle_elapsed_s,
                        startup_grace_s=self._inverter_params.startup_grace_s,
                        max_plausible_bias=self._inverter_params.max_plausible_bias,
                        now_s=now_ts,
                    )

            if self._hvac_mode == HVACMode.OFF:
                if (
                    self._schedule.enabled
                    and self._preset_mode == PRESET_SCHEDULE
                    and self._last_schedule_eval is not None
                    and self._last_schedule_eval.next_change_label
                ):
                    prefix = (
                        "coupé en dérogation"
                        if self._override_block_sig is not None
                        else "planning : coupé"
                    )
                    self._reason = (
                        f"{prefix} (prochain créneau à {self._last_schedule_eval.next_change_label})"
                    )
                else:
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

            # Application du délai anti-spam sur START/STOP
            if decision.action != HOLD and self._last_command is not None:
                if now - self._last_command < self._anti_spam:
                    decision = Decision(
                        HOLD,
                        f"temporisation anti-spam (encore {round((self._anti_spam - (now - self._last_command)).total_seconds())} s)",
                    )

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
        """Si quelqu'un agit directement sur le split (IR / appli), on s'aligne symétriquement."""
        split = self.hass.states.get(self._split)
        if split is None or split.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        is_heat = split.state == HVACMode.HEAT
        if is_heat:
            self._split_confirmed_heat = True

        if self._last_command is not None and now - self._last_command < ADOPT_GRACE:
            return

        if self._demand and not is_heat:
            # Protection latence Cloud : si l'état cloud n'a pas encore confirmé 'heat' mais que
            # la puissance électrique PAC confirme la marche dans les 5 premières minutes, on attend
            if (
                not self._split_confirmed_heat
                and self._last_command is not None
                and now - self._last_command < timedelta(seconds=300)
                and self._tracker is not None
                and self._tracker.is_on
            ):
                return

            _LOGGER.info(
                "%s : arrêt externe du split détecté (%s), thermostat virtuel basculé sur OFF",
                self.entity_id,
                split.state,
            )
            self._demand = False
            self._split_confirmed_heat = False
            self._cycle_started_at = None
            if self._hvac_mode == HVACMode.HEAT:
                self._hvac_mode = HVACMode.OFF
                if self._preset_mode == PRESET_SCHEDULE and self._schedule.enabled:
                    ev = self._schedule.evaluate(dt_util.as_local(now))
                    if ev.hvac_mode != "off":
                        self._override_block_sig = ev.block_signature
            if self._learning_enabled:
                self._learner.stop_heating(now.timestamp())
                self._schedule_learner_save()
            if self._tracker is not None:
                self._tracker.set_active(self._entry.entry_id, False)

        elif not self._demand and is_heat:
            _LOGGER.info(
                "%s : allumage externe du split détecté, prise en charge par la régulation",
                self.entity_id,
            )
            self._demand = True
            self._split_confirmed_heat = True
            self._cycle_started_at = now
            if self._hvac_mode == HVACMode.OFF:
                self._hvac_mode = HVACMode.HEAT
                if self._preset_mode == PRESET_SCHEDULE and self._schedule.enabled:
                    ev = self._schedule.evaluate(dt_util.as_local(now))
                    if ev.hvac_mode == "off":
                        self._override_block_sig = ev.block_signature
                        self._target = self._schedule.presets[PRESET_COMFORT]
                    elif ev.target_temp is not None:
                        self._target = ev.target_temp
            if self._learning_enabled and self._temp is not None:
                self._learner.start_cycle(self._target, self._temp)
            if self._tracker is not None:
                self._tracker.set_active(self._entry.entry_id, True)

    # ------------------------------------------------------------------ commandes vers le split
    async def _call(self, service: str, data: dict[str, Any]) -> bool:
        if self._split == self.entity_id:
            _LOGGER.error(
                "%s : auto-référence détectée sur %s, appel bloqué",
                self.entity_id,
                self._split,
            )
            return False
        try:
            await self.hass.services.async_call(
                "climate", service, {"entity_id": self._split, **data}, blocking=True
            )
            return True
        except HomeAssistantError as err:
            _LOGGER.error("%s : échec de climate.%s sur %s : %s", self.entity_id, service, self._split, err)
            return False

    def _split_setpoint_to_send(self, cycle_elapsed_s: float | None = None) -> float:
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

        if cycle_elapsed_s is None and self._cycle_started_at is not None:
            cycle_elapsed_s = max(
                0.0, (dt_util.utcnow() - self._cycle_started_at).total_seconds()
            )

        inverter_params = InverterParams(
            enabled=self._inverter_params.enabled,
            min_offset=self._inverter_params.min_offset,
            max_offset=self._inverter_params.max_offset,
            p_band=self._inverter_params.p_band,
            step=step,
            startup_grace_s=self._inverter_params.startup_grace_s,
            soft_start_error=self._inverter_params.soft_start_error,
            max_plausible_bias=self._inverter_params.max_plausible_bias,
        )

        setpoint, offset, mode_desc = compute_inverter_setpoint(
            room_temp=self._temp,
            target_temp=self._target,
            split_temp=split_temp,
            fallback_setpoint=self._split_setpoint,
            params=inverter_params,
            anticipation_offset=self._anticip_offset,
            learned_bias=self._learner.learned_bias,
            filtered_split_temp=self._learner.filtered_split_temp,
            cycle_elapsed_s=cycle_elapsed_s,
            outdoor_temp=self._outdoor_temp,
            min_temp=min_temp,
            max_temp=max_temp,
        )
        self._current_offset = offset
        self._inverter_mode_desc = mode_desc

        # N'apprendre le biais capteur qu'après au moins 10 min de régime établi (capot ventilé)
        if (
            self._learning_enabled
            and split_temp is not None
            and cycle_elapsed_s is not None
            and cycle_elapsed_s >= 600.0
            and abs(self._target - self._temp) <= 0.3
            and self._tracker
            and self._tracker.is_on
        ):
            if self._learner.update_steady_bias(split_temp, self._temp):
                self._schedule_learner_save()

        return setpoint

    async def _send_heat(self, cycle_elapsed_s: float | None = None) -> bool:
        split = self.hass.states.get(self._split)
        setpoint = self._split_setpoint_to_send(cycle_elapsed_s=cycle_elapsed_s)

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
        prev_start = self._cycle_started_at
        split = self.hass.states.get(self._split)
        was_heat = split is not None and split.state == HVACMode.HEAT
        self._cycle_started_at = now
        if await self._send_heat(cycle_elapsed_s=0.0):
            self._demand = True
            self._split_confirmed_heat = was_heat
            self._last_command = now
            self._user_target_changed = False
            self._tracker.set_active(self._entry.entry_id, True)
            if self._temp is not None:
                if self._learning_enabled:
                    self._learner.start_cycle(self._target, self._temp)
                else:
                    self._learner.filtered_split_temp = round(
                        self._temp + max(0.5, min(3.0, self._learner.learned_bias)), 2
                    )
            _LOGGER.debug("%s : démarrage (%s)", self.entity_id, self._reason)
        else:
            self._cycle_started_at = prev_start

    async def _stop(self, now: datetime, forced: bool = False) -> None:
        split = self.hass.states.get(self._split)
        modes = (split.attributes.get("hvac_modes") if split else None) or []
        if "off" in modes or not modes:
            ok = await self._call("set_hvac_mode", {"hvac_mode": "off"})
        else:
            ok = await self._call("turn_off", {})
        if ok:
            self._demand = False
            self._split_confirmed_heat = False
            self._cycle_started_at = None
            self._user_target_changed = False
            self._last_command = now
            self._tracker.set_active(self._entry.entry_id, False)
            if self._learning_enabled:
                self._learner.stop_heating(now.timestamp())
                self._schedule_learner_save()
            else:
                self._learner.filtered_split_temp = None
            _LOGGER.debug("%s : arrêt (%s)", self.entity_id, self._reason)

    async def _reassert(self, now: datetime) -> None:
        """Ajuste la consigne modulante vers le split et réaffirme périodiquement l'état."""
        if not self._demand:
            return
        if self._last_command is not None and now - self._last_command < self._anti_spam:
            return

        split = self.hass.states.get(self._split)
        if split is None or split.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        cycle_elapsed_s = (
            max(0.0, (now - self._cycle_started_at).total_seconds())
            if self._cycle_started_at is not None
            else None
        )
        resync_due = self._last_command is None or (now - self._last_command >= self._resync)

        # Si le split n'est toujours pas en mode HEAT après l'intervalle de resynchronisation
        if split.state != HVACMode.HEAT:
            if resync_due and await self._send_heat(cycle_elapsed_s=cycle_elapsed_s):
                self._last_command = now
                self._user_target_changed = False
                _LOGGER.debug("%s : réaffirmation périodique du mode chauffe", self.entity_id)
            return

        try:
            current = float(split.attributes.get(ATTR_TEMPERATURE))
        except (TypeError, ValueError):
            return

        wanted = self._split_setpoint_to_send(cycle_elapsed_s=cycle_elapsed_s)
        raw_split_temp = self._get_split_current_temp()
        hvac_action = split.attributes.get("hvac_action")

        # Relance anti-idle : après brassage initial (>= 4 min), si le split s'est mis en veille
        # ou n'a pas démarré car sa sonde interne a rattrapé la consigne envoyée.
        # Si un capteur de puissance est configuré, c'est lui qui fait foi :
        # si la PAC consomme (> 45 W), le compresseur tourne et n'est PAS en veille !
        anti_idle_rescue = False
        is_really_idle = (
            not self._tracker.is_on
            if self._tracker is not None
            else (hvac_action == HVACAction.IDLE)
        )
        if (
            cycle_elapsed_s is not None
            and cycle_elapsed_s >= 240.0
            and raw_split_temp is not None
            and is_really_idle
            and current < raw_split_temp + 1.0
        ):
            try:
                max_temp = float(split.attributes.get("max_temp", 30.0))
                step = float(split.attributes.get("target_temperature_step", 0.5)) or 0.5
            except (TypeError, ValueError):
                max_temp, step = 30.0, 0.5
            rescue_sp = round((raw_split_temp + self._inverter_params.min_offset) / step) * step
            if self._tracker is not None and self._tracker.is_on and self._temp is not None:
                cap = (
                    self._temp
                    + self._inverter_params.max_plausible_bias
                    + self._inverter_params.max_offset
                )
                rescue_sp = min(rescue_sp, round(cap / step) * step)
            else:
                rescue_sp = min(rescue_sp, 28.0)
            wanted = min(max_temp, max(wanted, round(rescue_sp, 1)))
            anti_idle_rescue = True

        urgent = self._user_target_changed or anti_idle_rescue
        if not urgent:
            # En démarrage doux progressif (phases 1 et 2), on autorise la montée douce de palier
            # mais on filtre les petits bruits
            if (
                cycle_elapsed_s is not None
                and cycle_elapsed_s < self._inverter_params.startup_grace_s
            ):
                if abs(current - wanted) < (step - 0.05):
                    return
            elif resync_due and abs(current - wanted) >= 0.5:
                # Réaffirmation périodique douce une fois resync_minutes écoulé
                pass
            else:
                # Temporisation de modulation lente (5 min par défaut)
                if (
                    self._last_command is not None
                    and now - self._last_command < self._modulation_interval
                ):
                    return
                # Zone morte calquée sur le pas de consigne (0.5 °C) pour permettre la modulation basse puissance (~500 W)
                if abs(current - wanted) < (step - 0.05):
                    return
        elif abs(current - wanted) < 0.4:
            self._user_target_changed = False
            return

        if await self._call("set_temperature", {ATTR_TEMPERATURE: wanted}):
            self._last_setpoint_sent = wanted
            self._last_command = now
            self._user_target_changed = False
            _LOGGER.debug(
                "%s : consigne modulée ajustée à %.1f °C (%s)",
                self.entity_id,
                wanted,
                "relance anti-idle" if anti_idle_rescue else self._inverter_mode_desc,
            )
