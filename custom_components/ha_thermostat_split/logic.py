"""Logique de décision pure, calcul inverter et apprentissage adaptatif anti-yoyo."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Any

START = "start"
STOP = "stop"
HOLD = "hold"


@dataclass(frozen=True)
class Params:
    """Paramètres de régulation."""

    cold_tolerance: float = 0.5
    hot_tolerance: float = 0.3
    min_on_s: float = 1800.0
    min_off_s: float = 300.0
    max_overshoot: float = 1.2
    max_undershoot: float = 1.5


@dataclass(frozen=True)
class InverterParams:
    """Paramètres de consigne dynamique inverter."""

    enabled: bool = True
    min_offset: float = 0.5
    max_offset: float = 2.0
    p_band: float = 1.5
    step: float = 0.5
    startup_grace_s: float = 360.0
    soft_start_error: float = 1.0
    max_plausible_bias: float = 3.5


@dataclass(frozen=True)
class Decision:
    """Décision prise et raison (affichée dans les attributs de l'entité)."""

    action: str
    reason: str


def _min(seconds: float) -> str:
    return f"{max(0.0, seconds) / 60:.0f} min"


def decide(
    *,
    heating: bool,
    temp: float,
    target: float,
    params: Params,
    compressor_on: bool,
    compressor_age_s: float | None,
    others_active: bool,
) -> Decision:
    """Décide s'il faut démarrer, arrêter ou ne rien faire.

    heating          : la pièce demande actuellement de la chaleur (split en chauffe)
    compressor_on    : état global du compresseur (puissance au-dessus du seuil)
    compressor_age_s : secondes depuis le dernier changement d'état du compresseur
                       (None = inconnu, traité comme « assez ancien »)
    others_active    : une autre pièce demande de la chaleur sur le même groupe
    """
    if not heating:
        if temp > target - params.cold_tolerance:
            return Decision(HOLD, "température suffisante")
        if compressor_on or others_active:
            return Decision(START, "besoin de chaleur, compresseur déjà en marche")
        if compressor_age_s is None or compressor_age_s >= params.min_off_s:
            return Decision(START, "besoin de chaleur")
        if temp <= target - params.max_undershoot:
            return Decision(START, "sous-chauffe trop importante, pause minimale ignorée")
        return Decision(
            HOLD,
            f"pause minimale en cours (encore {_min(params.min_off_s - compressor_age_s)})",
        )

    # la pièce chauffe
    if not compressor_on and temp >= target:
        return Decision(STOP, "annulation démarrage : cible déjà atteinte sans compresseur")
    if temp < target + params.hot_tolerance:
        return Decision(HOLD, "chauffe en cours")
    if others_active or not compressor_on:
        return Decision(STOP, "cible atteinte")
    if compressor_age_s is None or compressor_age_s >= params.min_on_s:
        return Decision(STOP, "cible atteinte, cycle minimal accompli")
    if temp >= target + params.max_overshoot:
        return Decision(STOP, "dépassement maximal atteint, cycle minimal ignoré")
    return Decision(
        HOLD,
        f"cible atteinte, cycle minimal en cours (encore {_min(params.min_on_s - compressor_age_s)})",
    )


def compute_inverter_setpoint(
    *,
    room_temp: float,
    target_temp: float,
    split_temp: float | None,
    fallback_setpoint: float,
    params: InverterParams,
    anticipation_offset: float = 0.0,
    learned_bias: float = 1.5,
    filtered_split_temp: float | None = None,
    cycle_elapsed_s: float | None = None,
    outdoor_temp: float | None = None,
    compressor_power_w: float | None = None,
    min_temp: float = 15.0,
    max_temp: float = 30.0,
) -> tuple[float, float, str]:
    """Calcule la consigne optimale à envoyer au split en mode inverter.

    Retourne : (consigne_arrondie, offset_applique, explication)
    """
    if not params.enabled or split_temp is None:
        clamped_fallback = min(max_temp, max(min_temp, fallback_setpoint))
        return (round(clamped_fallback, 1), 0.0, "consigne fixe de repli")

    error = target_temp - room_temp
    # L'anticipation d'inertie ne doit freiner qu'à l'approche de la cible (< 0.6 °C),
    # jamais quand la pièce est encore loin sous la consigne (ex: passage planning matin).
    if error >= 0.6:
        effective_anticipation = 0.0
    elif error > 0.3:
        effective_anticipation = anticipation_offset * ((0.6 - error) / 0.3)
    else:
        effective_anticipation = anticipation_offset
    effective_error = error - effective_anticipation
    in_startup = cycle_elapsed_s is not None and cycle_elapsed_s < params.startup_grace_s

    # 1. Température de référence stabilisée du split
    clamped_bias = max(0.5, min(3.0, learned_bias))
    # Au démarrage (< 180s), le capot contient souvent une poche d'air chaud résiduel (23-26 °C) :
    # on borne plus strictement la sonde brute avant que le ventilateur n'ait brassé l'air.
    if cycle_elapsed_s is not None and cycle_elapsed_s < 180.0:
        effective_max_bias = min(params.max_plausible_bias, max(1.5, clamped_bias + 0.5))
    else:
        effective_max_bias = params.max_plausible_bias

    high_power_approaching = (
        compressor_power_w is not None
        and compressor_power_w >= 1000.0
        and error <= 0.35
    )

    raw_clamped = min(
        room_temp + effective_max_bias,
        max(room_temp - 0.5, split_temp),
    )
    if filtered_split_temp is not None:
        filt_clamped = min(
            room_temp + effective_max_bias,
            max(room_temp - 0.5, filtered_split_temp),
        )
        # Ignore les micro-baisses (pas de 1 °C) sans jamais garder une mémoire haute > +0.4/+1.0 °C
        # En freinage haute puissance à l'approche de la cible, aucun maintien haut n'est appliqué
        if high_power_approaching:
            max_filter_hold = 0.0
        elif effective_error <= 0.1:
            max_filter_hold = 0.4
        else:
            max_filter_hold = 1.0
        ref_split_temp = min(max(filt_clamped, raw_clamped), raw_clamped + max_filter_hold)
    else:
        ref_split_temp = raw_clamped

    # 2. Calcul de l'offset dynamique proportionnel avec feed-forward météo et retour de puissance PAC
    weather_offset = 0.0
    if outdoor_temp is not None:
        if outdoor_temp < 8.0:
            weather_offset = min(0.5, max(0.0, (8.0 - outdoor_temp) * 0.04))
        elif outdoor_temp > 14.0:
            weather_offset = -0.2

    p_band = max(0.1, params.p_band)
    low_power_sluggish = (
        not in_startup
        and compressor_power_w is not None
        and 45.0 < compressor_power_w < 650.0
        and error >= 0.5
    )
    if low_power_sluggish:
        # Le compresseur tourne au ralenti (< 650 W) alors que la pièce est >= 0.5 °C sous la cible :
        # on force un offset franc pour sortir l'inverter de son palier basse fréquence (~435 W).
        if error >= 0.8:
            boost = 1.0
        elif error >= 0.6:
            boost = 0.5
        else:
            boost = 0.3
        base_max = max(params.max_offset, 2.0)
        offset = min(base_max + boost, max(4.0, params.max_offset + 1.0))
        mode = f"relance puissance ({compressor_power_w:.0f} W)"
    elif high_power_approaching:
        # La PAC pousse encore fort (>= 1000 W) alors qu'on approche de la cible (<= 0.35 °C) :
        # on réduit immédiatement l'offset au minimum pour freiner l'inertie et éviter tout yoyo.
        offset = max(0.2, params.min_offset + weather_offset)
        mode = f"freinage anti-dépassement ({compressor_power_w:.0f} W)"
    elif effective_error >= p_band:
        offset = params.max_offset
        mode = "chauffe rapide"
    elif effective_error <= 0.0:
        offset = max(0.2, params.min_offset + weather_offset)
        mode = "maintien / modulation minimale (~500 W)"
    else:
        ratio = effective_error / p_band
        base_offset = params.min_offset + ratio * (params.max_offset - params.min_offset)
        offset = min(params.max_offset, max(0.3, base_offset + weather_offset * (1.0 - ratio)))
        mode = f"modulation inverter ({ratio * 100:.0f}%)"

    raw_setpoint = ref_split_temp + offset

    # Plafonnement anti-emballement : en régime de croisière ou approche, la consigne envoyée
    # ne doit pas s'envoler si l'air sous plafond chauffe, mais elle ne doit JAMAIS être
    # inférieure à (ref_split_temp + min_offset), sinon le split coupe prématurément en thermo-off.
    cap_steady = max(ref_split_temp + params.min_offset, target_temp + clamped_bias + offset)
    raw_setpoint = min(raw_setpoint, cap_steady)

    # 3. Démarrage doux progressif anti-pic (> 2 kW)
    step = params.step if params.step > 0 else 0.5
    if in_startup:
        # En phase de démarrage, l'objectif est d'empêcher un saut thermique brutal
        # tout en assurant une consigne toujours supérieure à la sonde du split (+0.5 °C)
        # pour éviter que le split ne coupe prématurément en thermo-off (cycles courts).
        # - Palier 1 (0 à 180s) : consigne bridée à sonde_split + 0.5 °C
        # - Palier 2 (180 à grace_s) : consigne bridée à sonde_split + 1.0 °C
        split_ref = raw_clamped
        if cycle_elapsed_s < 180.0:
            soft_sp = round((split_ref + 0.5) / step) * step
            mode = "démarrage très doux (anti-pic phase 1)"
        else:
            soft_sp = round((split_ref + 1.0) / step) * step
            mode = "montée progressive (anti-pic phase 2)"
        raw_setpoint = min(raw_setpoint, soft_sp)

    rounded = round(raw_setpoint / step) * step

    # Plancher anti-thermo-off strict : la consigne arrondie doit rester au moins à +0.5 °C
    # au-dessus de la sonde du split bornée (sinon consigne == sonde_split => coupure compresseur)
    min_floor_offset = max(0.5, params.min_offset)
    if not in_startup and not high_power_approaching:
        if low_power_sluggish:
            min_floor_offset = max(min_floor_offset, 2.5 if step >= 1.0 else 2.0)
            if error >= 0.8:
                min_floor_offset = max(min_floor_offset, 3.2 if step >= 1.0 else 2.5)
        elif error >= 0.7:
            min_floor_offset = max(min_floor_offset, 2.2 if step >= 1.0 else 1.5)
        elif error >= 0.4:
            min_floor_offset = max(min_floor_offset, 1.2 if step >= 1.0 else 1.0)
    floor_sp = round((raw_clamped + min_floor_offset) / step) * step
    if floor_sp < raw_clamped + 0.45:
        floor_sp += step
    rounded = max(rounded, floor_sp)

    # En mode chauffe, la consigne envoyée ne doit pas être inférieure à la cible de la pièce
    # pour éviter qu'elle ne coupe prématurément
    final = min(max_temp, max(min_temp, max(target_temp, rounded)))

    return (round(final, 1), round(offset, 2), mode)


class AntiYoyoLearner:
    """Apprentissage statistique de l'inertie thermique, du biais capteur et de l'efficacité thermique."""

    def __init__(
        self,
        anticipation_h: float = 0.25,
        learned_bias: float = 1.5,
        min_anticipation_h: float = 0.05,
        max_anticipation_h: float = 0.8,
        tau_h: float | None = None,
        loss_rate_per_10k: float | None = None,
    ) -> None:
        self.anticipation_h = anticipation_h
        self.learned_bias = learned_bias
        self.min_anticipation_h = min_anticipation_h
        self.max_anticipation_h = max_anticipation_h
        self.tau_h = tau_h
        self.loss_rate_per_10k = loss_rate_per_10k
        self.outdoor_temp: float | None = None
        self.history_slope: deque[tuple[float, float]] = deque(maxlen=60)
        self.history_cooling: deque[tuple[float, float, float]] = deque(maxlen=60)
        self.cycle_count = 0
        self.last_overshoot: float | None = None
        self.filtered_split_temp: float | None = None
        self._last_split_filter_s: float | None = None
        self._cycle_peak_temp: float | None = None
        self._cycle_target: float | None = None
        self._in_cycle = False
        self._heating_stopped_at: float | None = None
        self._waiting_post_peak = False
        self._last_cooling_eval_s: float | None = None

    def add_sample(self, now_s: float, temp: float, hot_tolerance: float = 0.3) -> bool:
        """Enregistre un échantillon de température et suit le pic d'inertie.

        Retourne True si un cycle vient d'être finalisé (nouvelles valeurs apprises à persister).
        """
        self.history_slope.append((now_s, temp))

        if self._in_cycle:
            if self._cycle_peak_temp is None or temp > self._cycle_peak_temp:
                self._cycle_peak_temp = temp

            # Si le chauffage est arrêté, observer l'inertie post-chauffe
            if self._waiting_post_peak and self._heating_stopped_at is not None:
                elapsed = now_s - self._heating_stopped_at
                # Si plus de 15 min écoulées ou si la température redescend nettement sous le pic
                if elapsed >= 900.0 or temp <= (self._cycle_peak_temp - 0.15):
                    self._finalize_cycle(hot_tolerance)
                    return True
        return False

    def add_cooling_sample(
        self, now_s: float, room_temp: float, outdoor_temp: float | None
    ) -> bool:
        """Enregistre un échantillon hors-chauffe pour quantifier la déperdition thermique."""
        changed = False
        if self._in_cycle and self._waiting_post_peak and self._heating_stopped_at is not None:
            if self._cycle_peak_temp is None or room_temp > self._cycle_peak_temp:
                self._cycle_peak_temp = room_temp
            elapsed = now_s - self._heating_stopped_at
            if elapsed >= 900.0 or room_temp <= (self._cycle_peak_temp - 0.15):
                self._finalize_cycle(0.3)
                changed = True

        if outdoor_temp is None:
            return changed
        self.outdoor_temp = outdoor_temp
        # Si le chauffage est arrêté depuis au moins 15 min (inertie immédiate de surface dissipée)
        if not self._in_cycle and self._heating_stopped_at is not None:
            if now_s - self._heating_stopped_at >= 900.0:
                self.history_cooling.append((now_s, room_temp, outdoor_temp))
                if (
                    self._last_cooling_eval_s is None
                    or now_s - self._last_cooling_eval_s >= 1200.0
                ):
                    if self._evaluate_thermal_efficiency(now_s):
                        changed = True
        return changed

    def _evaluate_thermal_efficiency(self, now_s: float) -> bool:
        """Calcule la constante d'inertie tau (en heures) et le taux de déperdition spécifique."""
        if len(self.history_cooling) < 4:
            return False
        t_start, r_start, o_start = self.history_cooling[0]
        t_end, r_end, o_end = self.history_cooling[-1]
        dt_h = (t_end - t_start) / 3600.0
        if dt_h < 0.4:
            return False

        temp_drop = r_start - r_end
        delta_in_out = ((r_start + r_end) / 2.0) - ((o_start + o_end) / 2.0)

        # Gradient intérieur-extérieur significatif (>= 2 °C) et baisse mesurable
        if temp_drop >= 0.05 and delta_in_out >= 2.0:
            cool_rate_deg_h = temp_drop / dt_h
            loss_coeff = cool_rate_deg_h / delta_in_out
            tau_inst = 1.0 / max(0.005, loss_coeff)
            tau_clamped = max(5.0, min(120.0, tau_inst))
            if self.tau_h is None:
                self.tau_h = round(tau_clamped, 1)
            else:
                self.tau_h = round(0.15 * tau_clamped + 0.85 * self.tau_h, 1)
            self.loss_rate_per_10k = round(10.0 / self.tau_h, 2)
            self._last_cooling_eval_s = now_s
            return True
        return False

    @property
    def thermal_efficiency_label(self) -> str:
        """Diagnostic qualitatif de l'efficacité et de l'inertie thermique."""
        if self.tau_h is None:
            return "Apprentissage de l'inertie en cours"
        if self.tau_h >= 45.0:
            return f"Excellente inertie (tau = {self.tau_h:.0f}h)"
        if self.tau_h >= 30.0:
            return f"Bonne inertie thermique (tau = {self.tau_h:.0f}h)"
        if self.tau_h >= 18.0:
            return f"Inertie moyenne (tau = {self.tau_h:.0f}h)"
        return f"Faible inertie / Refroidissement rapide (tau = {self.tau_h:.0f}h)"

    def estimated_heat_need_w(self, target_temp: float) -> int | None:
        """Puissance thermique théorique de maintien estimée en Watts."""
        if self.outdoor_temp is None:
            return None
        delta = max(0.0, target_temp - self.outdoor_temp)
        tau = self.tau_h if self.tau_h is not None else 35.0
        loss_w_per_k = max(20.0, min(300.0, 4500.0 / tau))
        return int(round(loss_w_per_k * delta))

    def update_split_temp(
        self,
        split_temp: float | None,
        room_temp: float | None,
        cycle_elapsed_s: float | None = None,
        startup_grace_s: float = 300.0,
        max_plausible_bias: float = 3.5,
        now_s: float | None = None,
    ) -> float | None:
        """Lisse la sonde interne du split (ignore l'air chaud piégé et les sauts de 1 °C)."""
        if split_temp is None or room_temp is None:
            return self.filtered_split_temp

        clamped_bias = max(0.5, min(3.0, self.learned_bias))
        if cycle_elapsed_s is not None and cycle_elapsed_s < startup_grace_s:
            self.filtered_split_temp = round(room_temp + clamped_bias, 2)
            self._last_split_filter_s = now_s
            return self.filtered_split_temp

        clamped = min(room_temp + max_plausible_bias, max(room_temp - 0.5, split_temp))
        if self.filtered_split_temp is None:
            self.filtered_split_temp = round(clamped, 2)
            self._last_split_filter_s = now_s
            return self.filtered_split_temp

        # Filtrage asymétrique à constante de temps (insensible aux rafales d'événements HA) :
        # tau = 120 s à la hausse, tau = 900 s (15 min) à la baisse (anti-clignotement 1 °C / dégivrage)
        dt = (
            60.0
            if (now_s is None or self._last_split_filter_s is None)
            else max(0.0, min(600.0, now_s - self._last_split_filter_s))
        )
        self._last_split_filter_s = now_s
        tau = 120.0 if clamped >= self.filtered_split_temp else 900.0
        alpha = 1.0 - math.exp(-dt / tau)
        self.filtered_split_temp = round(
            alpha * clamped + (1.0 - alpha) * self.filtered_split_temp, 2
        )
        return self.filtered_split_temp

    def compute_slope(self, window_s: float = 900.0) -> float:
        """Calcule la vitesse d'évolution thermique récente en °C/h."""
        if len(self.history_slope) < 2:
            return 0.0
        now_s, cur_temp = self.history_slope[-1]
        cutoff = now_s - window_s
        candidates = [s for s in self.history_slope if (cutoff - window_s) <= s[0] <= cutoff]
        if not candidates:
            valid_recent = [s for s in self.history_slope if (now_s - s[0]) <= 2 * window_s]
            if len(valid_recent) < 2:
                return 0.0
            old_s, old_temp = valid_recent[0]
        else:
            old_s, old_temp = candidates[-1]

        dt = now_s - old_s
        if dt < 60.0:
            return 0.0
        slope = (cur_temp - old_temp) / (dt / 3600.0)
        return round(slope, 2)

    def get_anticipation_offset(self, slope_deg_h: float) -> float:
        """Offset d'anticipation (°C) pour amortir l'inertie thermique."""
        if slope_deg_h <= 0.0:
            return 0.0
        return round(self.anticipation_h * slope_deg_h, 3)

    def update_steady_bias(self, split_temp: float, room_temp: float) -> bool:
        """Met à jour le biais moyen split - pièce en régime de maintien stabilisé."""
        prev = self.learned_bias
        diff = max(0.0, min(3.5, split_temp - room_temp))
        updated = 0.05 * diff + 0.95 * self.learned_bias
        self.learned_bias = round(max(0.5, min(3.0, updated)), 2)
        return abs(self.learned_bias - prev) >= 0.01

    def start_cycle(self, target: float, initial_temp: float) -> None:
        """Démarre le suivi d'un cycle de chauffe."""
        # Si un cycle précédent attendait encore son pic, le finaliser d'abord
        if self._waiting_post_peak:
            self._finalize_cycle(0.3)

        self._in_cycle = True
        self._waiting_post_peak = False
        self._heating_stopped_at = None
        self._cycle_target = target
        self._cycle_peak_temp = initial_temp
        self.filtered_split_temp = round(
            initial_temp + max(0.5, min(3.0, self.learned_bias)), 2
        )
        self._last_split_filter_s = None

    def stop_heating(self, now_s: float) -> None:
        """Indique que la chauffe active s'est arrêtée, active l'observation du pic d'inertie."""
        self.filtered_split_temp = None
        self._last_split_filter_s = None
        self.history_cooling.clear()
        self._heating_stopped_at = now_s
        if self._in_cycle and not self._waiting_post_peak:
            self._waiting_post_peak = True

    def _finalize_cycle(self, hot_tolerance: float) -> None:
        """Calcule le dépassement final et ajuste l'anticipation."""
        if not self._in_cycle or self._cycle_target is None or self._cycle_peak_temp is None:
            self._in_cycle = False
            self._waiting_post_peak = False
            return

        overshoot = round(self._cycle_peak_temp - self._cycle_target, 2)
        self.last_overshoot = overshoot
        self.cycle_count += 1

        if overshoot > hot_tolerance:
            self.anticipation_h = min(self.max_anticipation_h, self.anticipation_h + 0.05)
        elif overshoot < 0.05:
            self.anticipation_h = max(self.min_anticipation_h, self.anticipation_h - 0.02)

        self.anticipation_h = round(self.anticipation_h, 3)
        self._in_cycle = False
        self._waiting_post_peak = False
        self._cycle_peak_temp = None
        self._cycle_target = None

    def to_dict(self) -> dict[str, Any]:
        """Sérialise pour sauvegarde permanente."""
        return {
            "anticipation_h": self.anticipation_h,
            "learned_bias": self.learned_bias,
            "cycle_count": self.cycle_count,
            "last_overshoot": self.last_overshoot,
            "tau_h": self.tau_h,
            "loss_rate_per_10k": self.loss_rate_per_10k,
        }

    def load_data(self, data: dict[str, Any]) -> None:
        """Restaure les données après un redémarrage."""
        if not isinstance(data, dict):
            return
        if "anticipation_h" in data:
            self.anticipation_h = max(
                self.min_anticipation_h,
                min(self.max_anticipation_h, float(data["anticipation_h"])),
            )
        if "learned_bias" in data:
            self.learned_bias = max(0.5, min(3.0, float(data["learned_bias"])))
        if "cycle_count" in data:
            self.cycle_count = int(data["cycle_count"])
        if "last_overshoot" in data and data["last_overshoot"] is not None:
            self.last_overshoot = float(data["last_overshoot"])
        if "tau_h" in data and data["tau_h"] is not None:
            self.tau_h = max(5.0, min(120.0, float(data["tau_h"])))
        if "loss_rate_per_10k" in data and data["loss_rate_per_10k"] is not None:
            self.loss_rate_per_10k = float(data["loss_rate_per_10k"])
