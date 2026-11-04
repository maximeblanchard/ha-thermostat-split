"""Logique de décision pure, calcul inverter et apprentissage adaptatif anti-yoyo."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
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
    max_overshoot: float = 1.0
    max_undershoot: float = 1.5


@dataclass(frozen=True)
class InverterParams:
    """Paramètres de consigne dynamique inverter."""

    enabled: bool = True
    min_offset: float = 0.5
    max_offset: float = 3.0
    p_band: float = 1.5
    step: float = 0.5


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
    effective_error = error - anticipation_offset

    p_band = max(0.1, params.p_band)
    if effective_error >= p_band:
        offset = params.max_offset
        mode = "chauffe rapide (forte puissance)"
    elif effective_error <= 0.0:
        offset = params.min_offset
        mode = "maintien / modulation minimale"
    else:
        ratio = effective_error / p_band
        offset = params.min_offset + ratio * (params.max_offset - params.min_offset)
        mode = f"modulation inverter ({ratio * 100:.0f}%)"

    raw_setpoint = split_temp + offset
    step = params.step if params.step > 0 else 0.5
    rounded = round(raw_setpoint / step) * step

    # En mode chauffe, la consigne envoyée ne doit jamais être inférieure à la cible de la pièce
    # pour éviter que le split ne coupe en croyant devoir refroidir
    final = min(max_temp, max(min_temp, max(target_temp, rounded)))

    return (round(final, 1), round(offset, 2), mode)


class AntiYoyoLearner:
    """Apprentissage statistique de l'inertie thermique et du biais capteur."""

    def __init__(
        self,
        anticipation_h: float = 0.25,
        learned_bias: float = 2.0,
        min_anticipation_h: float = 0.05,
        max_anticipation_h: float = 0.8,
    ) -> None:
        self.anticipation_h = anticipation_h
        self.learned_bias = learned_bias
        self.min_anticipation_h = min_anticipation_h
        self.max_anticipation_h = max_anticipation_h
        self.history_slope: deque[tuple[float, float]] = deque(maxlen=60)
        self.cycle_count = 0
        self.last_overshoot: float | None = None
        self._cycle_peak_temp: float | None = None
        self._cycle_target: float | None = None
        self._in_cycle = False
        self._heating_stopped_at: float | None = None
        self._waiting_post_peak = False

    def add_sample(self, now_s: float, temp: float, hot_tolerance: float = 0.3) -> None:
        """Enregistre un échantillon de température et suit le pic d'inertie."""
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

    def update_steady_bias(self, split_temp: float, room_temp: float) -> None:
        """Met à jour le biais moyen split - pièce en régime de maintien."""
        diff = split_temp - room_temp
        self.learned_bias = round(0.05 * diff + 0.95 * self.learned_bias, 2)

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

    def stop_heating(self, now_s: float) -> None:
        """Indique que la chauffe active s'est arrêtée, active l'observation du pic d'inertie."""
        if self._in_cycle and not self._waiting_post_peak:
            self._waiting_post_peak = True
            self._heating_stopped_at = now_s

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
        self._heating_stopped_at = None
        self._cycle_peak_temp = None
        self._cycle_target = None

    def to_dict(self) -> dict[str, Any]:
        """Sérialise pour sauvegarde permanente."""
        return {
            "anticipation_h": self.anticipation_h,
            "learned_bias": self.learned_bias,
            "cycle_count": self.cycle_count,
            "last_overshoot": self.last_overshoot,
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
            self.learned_bias = float(data["learned_bias"])
        if "cycle_count" in data:
            self.cycle_count = int(data["cycle_count"])
        if "last_overshoot" in data and data["last_overshoot"] is not None:
            self.last_overshoot = float(data["last_overshoot"])
