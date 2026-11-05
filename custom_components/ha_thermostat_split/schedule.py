"""Moteur de planification hebdomadaire et modes préréglés (logique pure sans dépendance HA)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

PRESET_NONE = "none"
PRESET_SCHEDULE = "schedule"
PRESET_COMFORT = "comfort"
PRESET_ECO = "eco"
PRESET_BOOST = "boost"
PRESET_AWAY = "away"

ALL_PRESETS: list[str] = [
    PRESET_SCHEDULE,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_BOOST,
    PRESET_AWAY,
    PRESET_NONE,
]

TEMP_PRESETS: tuple[str, ...] = (
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_BOOST,
    PRESET_AWAY,
)

SLOT_MODES: tuple[str, ...] = (
    "off",
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_BOOST,
    PRESET_AWAY,
    "temp",
)

DEFAULT_PRESET_TEMPS: dict[str, float] = {
    PRESET_COMFORT: 20.0,
    PRESET_ECO: 18.5,
    PRESET_BOOST: 21.5,
    PRESET_AWAY: 16.0,
}

DAY_NAMES_SHORT_FR: tuple[str, ...] = ("Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim")


def default_week_days() -> dict[str, list[dict[str, Any]]]:
    """Génère un planning hebdomadaire type prêt à l'emploi (Lun-Ven et Sam-Dim)."""
    weekday = [
        {"start": "00:00", "mode": PRESET_ECO, "temp": None},
        {"start": "06:30", "mode": PRESET_COMFORT, "temp": None},
        {"start": "08:30", "mode": PRESET_ECO, "temp": None},
        {"start": "17:30", "mode": PRESET_COMFORT, "temp": None},
        {"start": "22:30", "mode": PRESET_ECO, "temp": None},
    ]
    weekend = [
        {"start": "00:00", "mode": PRESET_ECO, "temp": None},
        {"start": "08:00", "mode": PRESET_COMFORT, "temp": None},
        {"start": "23:00", "mode": PRESET_ECO, "temp": None},
    ]
    days: dict[str, list[dict[str, Any]]] = {}
    for d in range(7):
        days[str(d)] = deepcopy(weekday if d < 5 else weekend)
    return days


def parse_hhmm(value: str) -> int:
    """Convertit 'HH:MM' en minutes depuis minuit (0..1439)."""
    if not isinstance(value, str) or ":" not in value:
        raise ValueError(f"Heure invalide : {value!r}")
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Heure invalide : {value!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Heure hors plage : {value!r}")
    return hour * 60 + minute


def format_hhmm(minutes: int) -> str:
    """Formate un nombre de minutes (0..1439) en 'HH:MM'."""
    clamped = max(0, min(1439, int(minutes)))
    return f"{clamped // 60:02d}:{clamped % 60:02d}"


def normalize_day_slots(
    raw_slots: Any,
    min_temp: float = 15.0,
    max_temp: float = 24.0,
) -> list[dict[str, Any]]:
    """Valide, trie et garantit une couverture complète 00:00 -> 24:00 sans trou."""
    if not isinstance(raw_slots, list) or not raw_slots:
        return [{"start": "00:00", "mode": PRESET_ECO, "temp": None}]

    by_min: dict[int, dict[str, Any]] = {}
    for item in raw_slots:
        if not isinstance(item, dict):
            continue
        try:
            m = parse_hhmm(str(item.get("start", "00:00")))
        except ValueError:
            continue
        mode = str(item.get("mode", PRESET_ECO))
        if mode not in SLOT_MODES:
            mode = PRESET_ECO
        temp_val: float | None = None
        if mode == "temp":
            try:
                raw_t = float(item.get("temp", 20.0))
            except (TypeError, ValueError):
                raw_t = 20.0
            rounded = round(raw_t * 2.0) / 2.0
            temp_val = max(min_temp, min(max_temp, rounded))
        by_min[m] = {
            "start": format_hhmm(m),
            "mode": mode,
            "temp": temp_val,
        }

    if not by_min:
        return [{"start": "00:00", "mode": PRESET_ECO, "temp": None}]

    sorted_mins = sorted(by_min.keys())
    if sorted_mins[0] != 0:
        first = by_min[sorted_mins[0]]
        by_min[0] = {
            "start": "00:00",
            "mode": first["mode"],
            "temp": first["temp"],
        }
        sorted_mins = sorted(by_min.keys())

    # Fusionner deux créneaux consécutifs strictement identiques sur la même journée
    cleaned: list[dict[str, Any]] = []
    for m in sorted_mins:
        slot = by_min[m]
        if (
            cleaned
            and cleaned[-1]["mode"] == slot["mode"]
            and cleaned[-1]["temp"] == slot["temp"]
        ):
            continue
        cleaned.append(slot)

    return cleaned


@dataclass(frozen=True)
class ScheduleEvaluation:
    """État résolu du planning à un instant donné."""

    hvac_mode: str                  # "heat" ou "off"
    target_temp: float | None       # None si hvac_mode == "off"
    slot_mode: str                  # "off", "comfort", "eco", "boost", "away", "temp"
    slot_start: str                 # "HH:MM" du début du créneau courant
    day_index: int                  # 0 (Lun) .. 6 (Dim)
    slot_index: int                 # index dans la liste du jour
    block_signature: str            # identifiant unique du bloc logique continu (gère minuit)
    next_change_label: str | None   # ex. "22:30" ou "Lun 06:30"
    next_hvac_mode: str | None
    next_target_temp: float | None


class WeeklySchedule:
    """Gestion complète du planning 7 jours et des températures associées aux presets."""

    def __init__(
        self,
        enabled: bool = False,
        presets: dict[str, float] | None = None,
        days: dict[str, list[dict[str, Any]]] | None = None,
        min_temp: float = 15.0,
        max_temp: float = 24.0,
    ) -> None:
        self.min_temp = min_temp
        self.max_temp = max_temp
        self.enabled = bool(enabled)
        self.presets: dict[str, float] = dict(DEFAULT_PRESET_TEMPS)
        if presets:
            self.update_presets(presets)
        self.days: dict[str, list[dict[str, Any]]] = default_week_days()
        if days:
            self.update_days(days)

    def update_bounds(self, min_temp: float, max_temp: float) -> None:
        self.min_temp = min_temp
        self.max_temp = max_temp
        self.update_presets(self.presets)
        self.update_days(self.days)

    def update_presets(self, raw_presets: dict[str, Any]) -> None:
        if not isinstance(raw_presets, dict):
            return
        for key in TEMP_PRESETS:
            if key in raw_presets:
                try:
                    val = round(float(raw_presets[key]) * 2.0) / 2.0
                    self.presets[key] = max(self.min_temp, min(self.max_temp, val))
                except (TypeError, ValueError):
                    pass

    def update_days(self, raw_days: dict[str, Any]) -> None:
        if not isinstance(raw_days, dict):
            return
        for d in range(7):
            key = str(d)
            if key in raw_days:
                self.days[key] = normalize_day_slots(
                    raw_days[key], min_temp=self.min_temp, max_temp=self.max_temp
                )

    def resolve_slot_action(self, slot: dict[str, Any]) -> tuple[str, float | None]:
        """Convertit un créneau en (hvac_mode, target_temp)."""
        mode = slot.get("mode", PRESET_ECO)
        if mode == "off":
            return ("off", None)
        if mode == "temp":
            raw_t = slot.get("temp")
            temp = float(raw_t) if raw_t is not None else self.presets[PRESET_COMFORT]
            return ("heat", max(self.min_temp, min(self.max_temp, temp)))
        preset_temp = self.presets.get(mode, self.presets[PRESET_COMFORT])
        return ("heat", max(self.min_temp, min(self.max_temp, preset_temp)))

    def evaluate(self, now_local: datetime) -> ScheduleEvaluation:
        """Évalue le créneau actif à l'heure locale donnée et calcule la prochaine transition."""
        day_idx = now_local.weekday()  # 0 = Lundi .. 6 = Dimanche
        now_min = now_local.hour * 60 + now_local.minute
        cur_date = now_local.date()

        day_slots = self.days.get(str(day_idx)) or normalize_day_slots([])
        slot_idx = 0
        for i, s in enumerate(day_slots):
            if parse_hhmm(s["start"]) <= now_min:
                slot_idx = i
            else:
                break

        cur_slot = day_slots[slot_idx]
        cur_action = self.resolve_slot_action(cur_slot)

        # 1. Marche arrière sur dates calendaires (insensible aux changements d'heure été/hiver DST)
        # pour trouver le début réel du bloc logique continu (évite de couper une dérogation à minuit)
        block_date = cur_date
        block_hhmm = cur_slot["start"]
        b_day = day_idx
        b_idx = slot_idx
        b_date = cur_date
        steps_back = 0
        while steps_back < 24:
            if b_idx > 0:
                prev_day = b_day
                prev_idx = b_idx - 1
                prev_date = b_date
            else:
                prev_day = (b_day - 1) % 7
                prev_slots = self.days.get(str(prev_day)) or normalize_day_slots([])
                prev_idx = len(prev_slots) - 1
                prev_date = b_date - timedelta(days=1)

            prev_slots_list = self.days.get(str(prev_day)) or normalize_day_slots([])
            prev_slot = prev_slots_list[prev_idx]
            if self.resolve_slot_action(prev_slot) != cur_action:
                break
            if (cur_date - prev_date).days >= 7:
                # Planning uniforme toute la semaine : ancrer sur aujourd'hui 00:00
                block_date = cur_date
                block_hhmm = "00:00"
                break

            b_day, b_idx, b_date = prev_day, prev_idx, prev_date
            block_date = b_date
            block_hhmm = prev_slot["start"]
            steps_back += 1

        temp_sig = "off" if cur_action[1] is None else f"{cur_action[1]:.1f}"
        block_signature = f"{block_date.isoformat()}T{block_hhmm}|{cur_action[0]}|{temp_sig}"

        # 2. Marche avant pour trouver la prochaine transition effective
        next_label: str | None = None
        next_hvac: str | None = None
        next_temp: float | None = None

        f_day = day_idx
        f_idx = slot_idx
        days_ahead = 0
        steps_fwd = 0
        while steps_fwd < 48 and days_ahead <= 7:
            f_slots = self.days.get(str(f_day)) or normalize_day_slots([])
            if f_idx + 1 < len(f_slots):
                f_idx += 1
            else:
                f_day = (f_day + 1) % 7
                f_idx = 0
                days_ahead += 1
                if days_ahead > 7:
                    break
                f_slots = self.days.get(str(f_day)) or normalize_day_slots([])

            cand_slot = f_slots[f_idx]
            cand_action = self.resolve_slot_action(cand_slot)
            if cand_action != cur_action:
                cand_hhmm = cand_slot["start"]
                if days_ahead == 0 or (
                    days_ahead == 1 and parse_hhmm(cand_hhmm) <= now_min
                ):
                    next_label = cand_hhmm
                else:
                    next_label = f"{DAY_NAMES_SHORT_FR[f_day]} {cand_hhmm}"
                next_hvac, next_temp = cand_action
                break
            steps_fwd += 1

        return ScheduleEvaluation(
            hvac_mode=cur_action[0],
            target_temp=cur_action[1],
            slot_mode=cur_slot["mode"],
            slot_start=cur_slot["start"],
            day_index=day_idx,
            slot_index=slot_idx,
            block_signature=block_signature,
            next_change_label=next_label,
            next_hvac_mode=next_hvac,
            next_target_temp=next_temp,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "presets": deepcopy(self.presets),
            "days": deepcopy(self.days),
        }

    def load_data(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if "enabled" in data:
            self.enabled = bool(data["enabled"])
        if "presets" in data:
            self.update_presets(data["presets"])
        if "days" in data:
            self.update_days(data["days"])
