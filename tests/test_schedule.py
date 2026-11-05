"""Tests unitaires du moteur de planification hebdomadaire et des dérogations."""
from datetime import datetime, timedelta, timezone, tzinfo
import importlib.util
import pathlib
import sys
import unittest

spec = importlib.util.spec_from_file_location(
    "schedule",
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ha_thermostat_split"
    / "schedule.py",
)
schedule_mod = importlib.util.module_from_spec(spec)
sys.modules["schedule"] = schedule_mod
spec.loader.exec_module(schedule_mod)

WeeklySchedule = schedule_mod.WeeklySchedule
normalize_day_slots = schedule_mod.normalize_day_slots
parse_hhmm = schedule_mod.parse_hhmm
format_hhmm = schedule_mod.format_hhmm
PRESET_COMFORT = schedule_mod.PRESET_COMFORT
PRESET_ECO = schedule_mod.PRESET_ECO
PRESET_BOOST = schedule_mod.PRESET_BOOST
PRESET_AWAY = schedule_mod.PRESET_AWAY


class NormalizationTests(unittest.TestCase):
    def test_empty_returns_full_day_eco(self):
        slots = normalize_day_slots([])
        self.assertEqual(slots, [{"start": "00:00", "mode": PRESET_ECO, "temp": None}])

    def test_fills_midnight_sorts_and_merges_duplicates(self):
        raw = [
            {"start": "18:00", "mode": PRESET_COMFORT},
            {"start": "07:00", "mode": PRESET_COMFORT},
            {"start": "09:00", "mode": PRESET_COMFORT},  # doublon consécutif -> fusionné
            {"start": "22:30", "mode": "off"},
        ]
        slots = normalize_day_slots(raw)
        self.assertEqual(
            slots,
            [
                {"start": "00:00", "mode": PRESET_COMFORT, "temp": None},
                {"start": "22:30", "mode": "off", "temp": None},
            ],
        )

    def test_temp_mode_clamps_and_rounds_half_degree(self):
        raw = [
            {"start": "00:00", "mode": "temp", "temp": 19.26},
            {"start": "12:00", "mode": "temp", "temp": 30.0},
            {"start": "18:00", "mode": "temp", "temp": 10.0},
        ]
        slots = normalize_day_slots(raw, min_temp=15.0, max_temp=24.0)
        self.assertEqual(slots[0]["temp"], 19.5)
        self.assertEqual(slots[1]["temp"], 24.0)
        self.assertEqual(slots[2]["temp"], 15.0)


class ScheduleEvaluationTests(unittest.TestCase):
    def test_default_weekday_transitions(self):
        ws = WeeklySchedule(enabled=True)
        # Lundi 07:15 -> Confort (20.0 °C), prochain changement à 08:30
        ev_morning = ws.evaluate(datetime(2026, 11, 2, 7, 15))
        self.assertEqual(ev_morning.hvac_mode, "heat")
        self.assertEqual(ev_morning.target_temp, 20.0)
        self.assertEqual(ev_morning.slot_mode, PRESET_COMFORT)
        self.assertEqual(ev_morning.next_change_label, "08:30")

    def test_midnight_rollover_preserves_override_block_signature(self):
        """Une dérogation lancée à 23h30 (Éco) ne doit pas sauter à 00h00 si Mardi 00h00 est aussi en Éco."""
        ws = WeeklySchedule(enabled=True)
        ev_late_mon = ws.evaluate(datetime(2026, 11, 2, 23, 30))  # Lundi 23:30 (Éco depuis 22:30)
        ev_early_tue = ws.evaluate(datetime(2026, 11, 3, 0, 15))  # Mardi 00:15 (Éco depuis 00:00)
        ev_morning_tue = ws.evaluate(datetime(2026, 11, 3, 6, 30))  # Mardi 06:30 (Confort)

        self.assertEqual(ev_late_mon.block_signature, ev_early_tue.block_signature)
        self.assertEqual(ev_late_mon.next_change_label, "06:30")
        self.assertEqual(ev_early_tue.next_change_label, "06:30")
        self.assertNotEqual(ev_early_tue.block_signature, ev_morning_tue.block_signature)

    def test_off_and_custom_temp_slots(self):
        ws = WeeklySchedule(
            enabled=True,
            days={
                "0": [
                    {"start": "00:00", "mode": "off", "temp": None},
                    {"start": "06:00", "mode": "temp", "temp": 20.5},
                    {"start": "09:00", "mode": "off", "temp": None},
                ]
            },
        )
        ev_night = ws.evaluate(datetime(2026, 11, 2, 3, 0))
        self.assertEqual(ev_night.hvac_mode, "off")
        self.assertIsNone(ev_night.target_temp)
        self.assertEqual(ev_night.next_change_label, "06:00")

        ev_day = ws.evaluate(datetime(2026, 11, 2, 7, 0))
        self.assertEqual(ev_day.hvac_mode, "heat")
        self.assertEqual(ev_day.target_temp, 20.5)
        self.assertEqual(ev_day.next_change_label, "09:00")

    def test_dst_switchover_preserves_block_signature(self):
        """Vérifie que le passage à l'heure d'hiver/été ne décale pas block_signature d'une heure."""

        class MockParisDST(tzinfo):
            def utcoffset(self, dt):
                # Simule le passage heure d'été (+02:00) -> heure d'hiver (+01:00) le 25 oct 2026
                if dt is not None and (dt.month, dt.day) >= (10, 25) and dt.hour >= 3:
                    return timedelta(hours=1)
                return timedelta(hours=2)

            def dst(self, dt):
                return timedelta(0)

        tz = MockParisDST()
        ws = WeeklySchedule(enabled=True)
        # Samedi 24 oct 2026 à 23:30 (+02:00) : créneau Éco commencé à 23:00
        ev_sat = ws.evaluate(datetime(2026, 10, 24, 23, 30, tzinfo=tz))
        # Dimanche 25 oct 2026 à 04:00 (+01:00, après changement d'heure) : toujours le même bloc Éco !
        ev_sun = ws.evaluate(datetime(2026, 10, 25, 4, 0, tzinfo=tz))
        self.assertEqual(ev_sat.block_signature, ev_sun.block_signature)
        self.assertTrue(ev_sun.block_signature.startswith("2026-10-24T23:00|"))

    def test_serialization_and_preset_updates(self):
        ws = WeeklySchedule(enabled=True, presets={PRESET_COMFORT: 21.0, PRESET_ECO: 18.0})
        payload = ws.to_dict()
        restored = WeeklySchedule()
        restored.load_data(payload)
        self.assertTrue(restored.enabled)
        self.assertEqual(restored.presets[PRESET_COMFORT], 21.0)
        self.assertEqual(restored.presets[PRESET_ECO], 18.0)


if __name__ == "__main__":
    unittest.main()
