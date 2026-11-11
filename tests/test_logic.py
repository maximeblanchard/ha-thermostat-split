"""Tests de la logique de décision, du calcul inverter et de l'apprentissage anti-yoyo."""
import importlib.util
import pathlib
import unittest

spec = importlib.util.spec_from_file_location(
    "logic",
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ha_thermostat_split"
    / "logic.py",
)
logic = importlib.util.module_from_spec(spec)
import sys

sys.modules["logic"] = logic
spec.loader.exec_module(logic)

decide = logic.decide
Params = logic.Params
InverterParams = logic.InverterParams
compute_inverter_setpoint = logic.compute_inverter_setpoint
AntiYoyoLearner = logic.AntiYoyoLearner
START = logic.START
STOP = logic.STOP
HOLD = logic.HOLD

P = Params(
    cold_tolerance=0.5,
    hot_tolerance=0.3,
    min_on_s=1800,
    min_off_s=300,
    max_overshoot=1.0,
    max_undershoot=1.5,
)


def d(**kw):
    base = dict(
        heating=False,
        temp=20.0,
        target=20.0,
        params=P,
        compressor_on=False,
        compressor_age_s=1000.0,
        others_active=False,
    )
    base.update(kw)
    return decide(**base)


class Idle(unittest.TestCase):
    def test_warm_enough(self):
        self.assertEqual(d(temp=19.6).action, HOLD)

    def test_cold_after_long_pause_starts(self):
        self.assertEqual(d(temp=19.4, compressor_age_s=600).action, START)

    def test_cold_during_min_pause_waits(self):
        self.assertEqual(d(temp=19.4, compressor_age_s=100).action, HOLD)

    def test_very_cold_overrides_min_pause(self):
        self.assertEqual(d(temp=18.4, compressor_age_s=100).action, START)

    def test_compressor_already_on_starts_freely(self):
        self.assertEqual(d(temp=19.4, compressor_on=True, compressor_age_s=10).action, START)

    def test_other_room_active_starts_freely(self):
        self.assertEqual(d(temp=19.4, others_active=True, compressor_age_s=10).action, START)

    def test_unknown_age_is_old(self):
        self.assertEqual(d(temp=19.4, compressor_age_s=None).action, START)


class Heating(unittest.TestCase):
    def h(self, **kw):
        kw.setdefault("compressor_age_s", 600)
        return d(heating=True, compressor_on=True, **kw)

    def test_below_target_keeps_heating(self):
        self.assertEqual(self.h(temp=20.2).action, HOLD)

    def test_reached_but_min_cycle_not_done(self):
        self.assertEqual(self.h(temp=20.4).action, HOLD)

    def test_reached_and_min_cycle_done(self):
        self.assertEqual(self.h(temp=20.4, compressor_age_s=2000).action, STOP)

    def test_overshoot_cap_stops_early(self):
        self.assertEqual(self.h(temp=21.0).action, STOP)

    def test_other_room_active_stop_is_free(self):
        self.assertEqual(self.h(temp=20.4, others_active=True).action, STOP)

    def test_compressor_not_running_stop_is_free(self):
        self.assertEqual(
            d(heating=True, compressor_on=False, compressor_age_s=60, temp=20.4).action,
            STOP,
        )

    def test_abort_false_start_when_target_reached_before_compressor_runs(self):
        # Si la demande est active mais que le compresseur n'a pas encore démarré
        # et que la température est déjà à la cible (ex: retour de capteurs réveillés après le boot),
        # on annule immédiatement la chauffe sans attendre le compresseur
        dec = d(heating=True, compressor_on=False, compressor_age_s=10, temp=20.0, target=20.0)
        self.assertEqual(dec.action, STOP)
        self.assertIn("annulation démarrage", dec.reason)

        # Si par contre la température est sous la cible, on maintient la mise en route
        dec_cold = d(heating=True, compressor_on=False, compressor_age_s=10, temp=19.8, target=20.0)
        self.assertEqual(dec_cold.action, HOLD)

    def test_unknown_age_is_old(self):
        self.assertEqual(
            d(heating=True, compressor_on=True, compressor_age_s=None, temp=20.4).action,
            STOP,
        )


class InverterModulation(unittest.TestCase):
    def setUp(self):
        self.params = InverterParams(
            enabled=True,
            min_offset=0.5,
            max_offset=2.0,
            p_band=1.5,
            step=0.5,
            startup_grace_s=360.0,
        )

    def test_fallback_when_split_temp_none(self):
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=19.0,
            target_temp=20.0,
            split_temp=None,
            fallback_setpoint=24.0,
            params=self.params,
        )
        self.assertEqual(sp, 24.0)
        self.assertEqual(offset, 0.0)

    def test_fast_heating_when_far_below(self):
        # Temp pièce = 18.0, Cible = 20.0 (erreur 2.0 >= p_band 1.5)
        # Split mesure 20.0 -> offset = max_offset (2.0) -> setpoint = 22.0
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=18.0,
            target_temp=20.0,
            split_temp=20.0,
            fallback_setpoint=24.0,
            params=self.params,
        )
        self.assertEqual(sp, 22.0)
        self.assertEqual(offset, 2.0)
        self.assertIn("chauffe rapide", desc)

    def test_setpoint_never_below_room_target(self):
        # Si le split mesure anormalement froid (ex: 17°C) alors que la cible est 20°C
        # La consigne envoyée ne doit pas descendre sous 20°C pour ne pas couper le split
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=19.9,
            target_temp=20.0,
            split_temp=17.0,
            fallback_setpoint=24.0,
            params=self.params,
        )
        self.assertGreaterEqual(sp, 20.0)

    def test_minimum_modulation_prevents_integer_idle(self):
        # Temp pièce = 20.0, Cible = 20.0 (erreur 0.0)
        # Split mesure 21.0 -> setpoint = 21.0 + 0.5 = 21.5 (sweet-spot 500 W prouvé empiriquement)
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=20.0,
            target_temp=20.0,
            split_temp=21.0,
            fallback_setpoint=24.0,
            params=self.params,
        )
        self.assertEqual(sp, 21.5)
        self.assertEqual(offset, 0.5)
        self.assertIn("modulation minimale", desc)

    def test_startup_phase_1_ultra_soft(self):
        # Scénario réel CSV : pièce à 18.2 °C (cible 19.0 °C), capot split avec chaleur piégée à 26.0 °C
        # Au démarrage (cycle_elapsed_s=0), on ne doit PAS envoyer 26.5 °C ni s'effondrer sous la consigne.
        # Le capteur split brut est borné par max_plausible_bias (+3.5 °C) pour éviter de s'emballer
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=18.2,
            target_temp=19.0,
            split_temp=26.0,
            fallback_setpoint=24.0,
            params=self.params,
            learned_bias=1.8,
            cycle_elapsed_s=0.0,
        )
        self.assertLessEqual(sp, 23.0)
        self.assertGreaterEqual(sp, 19.0)
        self.assertIn("anti-pic phase 1", desc)

    def test_startup_phase_2_progressive_ramp(self):
        # À t=200s (phase 2), l'échangeur est tiédi, on autorise une montée progressive
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=18.3,
            target_temp=19.0,
            split_temp=19.0,
            fallback_setpoint=24.0,
            params=self.params,
            learned_bias=1.8,
            cycle_elapsed_s=200.0,
        )
        self.assertEqual(sp, 20.0)
        self.assertIn("anti-pic phase 2", desc)

    def test_low_power_wakeup_boost(self):
        # Passage planning à 19.5°C alors que la pièce est à 18.4°C (erreur = 1.1°C)
        # et que le compresseur stagne au palier mini (~435 W) depuis > 10 min :
        # doit envoyer un boost franc (+2.0°C -> 22.0°C) et ignorer l'anticipation parasites
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=18.4,
            target_temp=19.5,
            split_temp=20.0,
            fallback_setpoint=24.0,
            params=self.params,
            anticipation_offset=0.4,
            cycle_elapsed_s=3600.0,
            compressor_power_w=435.0,
        )
        self.assertGreaterEqual(sp, 22.0)
        self.assertIn("relance puissance", desc)

    def test_low_power_morning_step_boost_with_integer_step(self):
        # Cas réel utilisateur : split avec pas entier (step=1.0), offset max configuré à 3.0°C.
        # Passage nuit (18.5°C) -> matin (19.5°C) alors que la PAC tournait à 450 W.
        # La consigne envoyée doit sauter de 21°C à 24°C pour sortir l'inverter du seuil plancher 450 W.
        params3 = InverterParams(
            enabled=True,
            min_offset=0.5,
            max_offset=3.0,
            p_band=1.5,
            step=1.0,
            startup_grace_s=360.0,
            soft_start_error=1.2,
            max_plausible_bias=3.0,
        )
        # Nuit : consigne envoyée = 21.0°C
        sp_night, _, _ = compute_inverter_setpoint(
            room_temp=18.5,
            target_temp=18.5,
            split_temp=20.3,
            fallback_setpoint=20.0,
            params=params3,
            cycle_elapsed_s=7200.0,
            compressor_power_w=450.0,
        )
        self.assertEqual(sp_night, 21.0)

        # Matin : consigne boostée à 24.0°C (+3.0°C au split, delta sonde interne +3.7°C)
        sp_morn, offset, desc = compute_inverter_setpoint(
            room_temp=18.5,
            target_temp=19.5,
            split_temp=20.3,
            fallback_setpoint=20.0,
            params=params3,
            cycle_elapsed_s=7300.0,
            compressor_power_w=450.0,
        )
        self.assertEqual(sp_morn, 24.0)
        self.assertGreaterEqual(offset, 3.5)
        self.assertIn("relance puissance", desc)

    def test_startup_target_floor_prevents_mild_weather_thermo_off(self):
        # Cas réel utilisateur par temps doux (11°C à 15°C ext) :
        # Démarrage le matin à 07:45 : pièce à 18.1°C, cible 19.5°C, sonde split froide à 17.0°C.
        # En phase de démarrage doux (t=60s), la consigne envoyée ne doit PAS être bridée à 19.5°C
        # sinon la bouffée de chaleur initiale (2.5 kW) fait monter le capot à 20°C et coupe en thermo-off (cycle de 6 min).
        # La consigne doit être au moins à 21.0°C pour laisser le compresseur passer son pic sans couper.
        params_int = InverterParams(
            enabled=True,
            min_offset=0.5,
            max_offset=3.0,
            p_band=1.5,
            step=1.0,
            startup_grace_s=360.0,
            soft_start_error=1.2,
            max_plausible_bias=3.0,
        )
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=18.1,
            target_temp=19.5,
            split_temp=17.0,
            fallback_setpoint=20.0,
            params=params_int,
            cycle_elapsed_s=60.0,
            compressor_power_w=800.0,
        )
        self.assertGreaterEqual(sp, 21.0)
        self.assertIn("anti-pic", desc)

    def test_strict_anti_thermo_off_floor(self):
        # Même si filtered_split_temp retarde (20.2°C) alors que split_temp vient de monter à 21.0°C,
        # la consigne arrondie ne doit JAMAIS tomber à <= 21.0°C (évite le thermo-off immédiat)
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=18.9,
            target_temp=19.0,
            split_temp=21.0,
            filtered_split_temp=20.2,
            fallback_setpoint=24.0,
            params=self.params,
            cycle_elapsed_s=900.0,
        )
        self.assertGreaterEqual(sp, 21.5)

    def test_high_power_approaching_braking(self):
        # Si la PAC pousse encore fort (>= 1000 W) alors qu'on approche à <= 0.35°C de la cible,
        # on réduit immédiatement l'offset à +0.5°C pour éviter le dépassement (+0.6°C)
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=19.2,
            target_temp=19.5,
            split_temp=21.0,
            filtered_split_temp=22.5,
            fallback_setpoint=24.0,
            params=self.params,
            cycle_elapsed_s=500.0,
            compressor_power_w=1300.0,
        )
        self.assertEqual(sp, 21.5)
        self.assertIn("freinage anti-dépassement", desc)

    def test_anticipation_shifts_modulation(self):
        # Temp pièce = 19.8, Cible = 20.0. Erreur = 0.2
        # Mais avec anticipation_offset = 0.3 -> erreur effective = -0.1 <= 0
        # Donc doit basculer en modulation minimale sans attendre le dépassement !
        sp, offset, desc = compute_inverter_setpoint(
            room_temp=19.8,
            target_temp=20.0,
            split_temp=21.0,
            fallback_setpoint=24.0,
            params=self.params,
            anticipation_offset=0.3,
        )
        self.assertEqual(offset, 0.5)
        self.assertEqual(sp, 21.5)
        self.assertIn("modulation minimale", desc)


    def test_outdoor_weather_feedforward(self):
        # Temp pièce = 20.0, Cible = 20.0 (erreur 0.0)
        # Par temps doux (15°C ext), maintien = 0.5°C
        _, off_mild, _ = compute_inverter_setpoint(
            room_temp=20.0,
            target_temp=20.0,
            split_temp=21.0,
            fallback_setpoint=24.0,
            params=self.params,
            outdoor_temp=15.0,
        )
        self.assertLessEqual(off_mild, 0.5)

        # Par grand froid (0°C ext), l'anticipation météo feed-forward soutient l'allure de croisière
        _, off_cold, _ = compute_inverter_setpoint(
            room_temp=20.0,
            target_temp=20.0,
            split_temp=21.0,
            fallback_setpoint=24.0,
            params=self.params,
            outdoor_temp=0.0,
        )
        self.assertGreater(off_cold, off_mild)
        self.assertAlmostEqual(off_cold, 0.82, delta=0.05)

    def test_thermal_efficiency_learning(self):
        learner = AntiYoyoLearner()
        # Chauffage arrêté à t=0
        learner.stop_heating(0.0)

        # Les 15 premières minutes sont ignorées (inertie de surface)
        learner.add_cooling_sample(300.0, 19.5, 5.0)
        self.assertIsNone(learner.tau_h)

        # De t=900s à t=4500s (1h de refroidissement régulier : baisse de 19.5 à 19.3 °C par 5°C ext)
        learner.add_cooling_sample(900.0, 19.5, 5.0)
        learner.add_cooling_sample(1800.0, 19.45, 5.0)
        learner.add_cooling_sample(2700.0, 19.40, 5.0)
        ok1 = learner.add_cooling_sample(3600.0, 19.35, 5.0)
        ok2 = learner.add_cooling_sample(4800.0, 19.30, 5.0)
        self.assertTrue(ok1 and ok2)
        self.assertIsNotNone(learner.tau_h)
        self.assertGreater(learner.tau_h, 30.0)  # Bonne ou excellente inertie
        self.assertIn("tau =", learner.thermal_efficiency_label)

        # Persistance et restauration
        saved = learner.to_dict()
        self.assertIn("tau_h", saved)
        new_learner = AntiYoyoLearner()
        new_learner.load_data(saved)
        self.assertEqual(new_learner.tau_h, learner.tau_h)


class AntiYoyoLearning(unittest.TestCase):
    def test_slope_and_anticipation(self):
        learner = AntiYoyoLearner(anticipation_h=0.25)
        # 19.0°C à t=0, 20.0°C à t=3600s (montée de 1.0°C/h)
        learner.add_sample(0.0, 19.0)
        learner.add_sample(3600.0, 20.0)
        slope = learner.compute_slope(window_s=3600.0)
        self.assertEqual(slope, 1.0)
        anticip = learner.get_anticipation_offset(slope)
        self.assertEqual(anticip, 0.25)

    def test_split_temp_asymmetric_filter_ignores_flicker_and_defrost(self):
        learner = AntiYoyoLearner(learned_bias=1.5)
        learner.start_cycle(target=19.5, initial_temp=19.0)
        # Après stabilisation à 21 °C (t=600s -> t=1200s)
        learner.update_split_temp(21.0, 19.5, cycle_elapsed_s=600.0, now_s=600.0)
        learner.update_split_temp(21.0, 19.5, cycle_elapsed_s=900.0, now_s=900.0)
        # Clignotement entier 21 -> 20 pendant 4 secondes (scénario 08h14)
        filt = learner.update_split_temp(20.0, 19.5, cycle_elapsed_s=904.0, now_s=904.0)
        self.assertGreaterEqual(filt, 20.8)
        # Dégivrage de 3 minutes : split_temp tombe à 19 °C pendant 180s (scénario 05h17)
        filt_defrost = learner.update_split_temp(19.0, 19.0, cycle_elapsed_s=1084.0, now_s=1084.0)
        self.assertGreaterEqual(filt_defrost, 20.4)

    def test_steady_bias_clamps_anomalies(self):
        learner = AntiYoyoLearner(learned_bias=1.5)
        learner.update_steady_bias(split_temp=28.0, room_temp=19.5)
        self.assertLessEqual(learner.learned_bias, 3.0)

    def test_cycle_adaptation_overshoot(self):
        learner = AntiYoyoLearner(anticipation_h=0.25)
        learner.start_cycle(target=20.0, initial_temp=19.0)
        self.assertFalse(learner.add_sample(100.0, 19.5))
        self.assertFalse(learner.add_sample(200.0, 20.5))  # Dépassement de 0.5°C (> hot_tol 0.3)
        learner.stop_heating(200.0)
        # La température commence à redescendre (20.3 < 20.5 - 0.15), ce qui finalise le cycle
        finalized = learner.add_sample(300.0, 20.3, hot_tolerance=0.3)
        self.assertTrue(finalized)
        # L'anticipation doit avoir augmenté pour freiner plus tôt au prochain cycle
        self.assertGreater(learner.anticipation_h, 0.25)
        self.assertEqual(learner.last_overshoot, 0.5)

    def test_serialization(self):
        learner = AntiYoyoLearner(anticipation_h=0.35, learned_bias=2.4)
        data = learner.to_dict()
        new_learner = AntiYoyoLearner()
        new_learner.load_data(data)
        self.assertEqual(new_learner.anticipation_h, 0.35)
        self.assertEqual(new_learner.learned_bias, 2.4)


class Simulation(unittest.TestCase):
    """Modèle thermique simple à deux nœuds : vérifie l'invariant du cycle minimal."""

    def run_sim(self, outdoor, min_on_min, max_overshoot=1.0):
        params = Params(0.5, 0.3, min_on_min * 60, 300, max_overshoot, 1.5)
        dt = 60.0
        air, wall = 19.0, 18.0
        c_air, c_wall = 0.9e6, 12e6
        g_aw, g_out = 600.0, 75.0
        heating, comp_on, on_since, off_since = False, False, 0, None
        pause_left = 0
        stops, durations = [], []
        for step in range(24 * 60):
            q = 0.0
            if heating and pause_left <= 0:
                q = 2300.0
                if not comp_on:
                    comp_on, on_since = True, step
            elif comp_on and not heating:
                comp_on, off_since = False, step
                durations.append(step - on_since)
                pause_left = 4
            pause_left -= 1
            air += (
                dt
                * (q - g_aw * (air - wall) - 0.25 * g_out * (air - outdoor))
                / c_air
            )
            wall += (
                dt * (g_aw * (air - wall) - 0.75 * g_out * (wall - outdoor)) / c_wall
            )
            age = (
                (step - on_since) * dt
                if comp_on
                else (None if off_since is None else (step - off_since) * dt)
            )
            res = decide(
                heating=heating,
                temp=air,
                target=20.0,
                params=params,
                compressor_on=comp_on,
                compressor_age_s=age,
                others_active=False,
            )
            if res.action == START:
                heating = True
            elif res.action == STOP:
                stops.append((comp_on, age, air))
                heating = False
        return stops, durations

    def test_invariant(self):
        for outdoor in (2.0, 8.0, 14.0):
            stops, _ = self.run_sim(outdoor, 30)
            self.assertTrue(stops)
            for comp_on, age, air in stops:
                ok = (not comp_on) or age >= 1800 or air >= 21.0
                self.assertTrue(ok, (outdoor, comp_on, age, air))


if __name__ == "__main__":
    unittest.main()
