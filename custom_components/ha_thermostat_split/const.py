"""Constantes de l'intégration HA Thermostat Split."""
from __future__ import annotations

DOMAIN = "ha_thermostat_split"
VERSION = "0.5.0"

CONF_NAME = "name"
CONF_SPLIT = "split_entity"
CONF_SENSORS = "temperature_sensors"
CONF_POWER = "power_sensor"
CONF_POWER_THRESHOLD = "compressor_on_threshold"
CONF_MIN_ON = "min_on_minutes"
CONF_MIN_OFF = "min_off_minutes"
CONF_COLD_TOL = "cold_tolerance"
CONF_HOT_TOL = "hot_tolerance"
CONF_MAX_OVERSHOOT = "max_overshoot"
CONF_MAX_UNDERSHOOT = "max_undershoot"
CONF_SPLIT_SETPOINT = "split_setpoint"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_TARGET_DEFAULT = "target_default"
CONF_SENSOR_MAX_AGE = "sensor_max_age_minutes"
CONF_FAILSAFE = "failsafe_minutes"
CONF_RESYNC = "resync_minutes"
CONF_ANTI_SPAM = "anti_spam_seconds"
CONF_MODULATION_INTERVAL = "modulation_interval_seconds"
CONF_OUTDOOR_TEMP = "outdoor_temperature_sensor"
CONF_INVERTER_MODE = "inverter_mode"
CONF_INVERTER_MIN_OFFSET = "inverter_min_offset"
CONF_INVERTER_MAX_OFFSET = "inverter_max_offset"
CONF_INVERTER_P_BAND = "inverter_p_band"
CONF_LEARNING_ENABLED = "learning_enabled"

DEFAULTS: dict = {
    CONF_POWER_THRESHOLD: 45.0,   # W : au-dessus, le compresseur est considéré en marche
    CONF_MIN_ON: 30.0,            # min : cible de durée de marche minimale
    CONF_MIN_OFF: 5.0,            # min : cible de pause minimale
    CONF_COLD_TOL: 0.5,           # °C sous la cible pour démarrer
    CONF_HOT_TOL: 0.3,            # °C au-dessus de la cible pour arrêter
    CONF_MAX_OVERSHOOT: 1.2,      # °C : dépassement maxi toléré (absorbe la bouffée d'air chaud au démarrage)
    CONF_MAX_UNDERSHOOT: 1.5,     # °C : sous-chauffe maxi toléré pendant la pause minimale
    CONF_SPLIT_SETPOINT: 24.0,    # °C statiques de repli envoyés au split
    CONF_MIN_TEMP: 15.0,
    CONF_MAX_TEMP: 24.0,
    CONF_TARGET_DEFAULT: 20.0,
    CONF_SENSOR_MAX_AGE: 90.0,    # min : capteur ignoré au-delà
    CONF_FAILSAFE: 30.0,          # min sans capteur valide avant arrêt de sécurité
    CONF_RESYNC: 10.0,            # min : réaffirmation de la consigne envoyée au split
    CONF_ANTI_SPAM: 90.0,         # s : délai minimal entre START/STOP ou actions urgentes
    CONF_MODULATION_INTERVAL: 300.0,  # s : délai minimal (5 min) entre deux ajustements de consigne inverter
    CONF_OUTDOOR_TEMP: None,      # Capteur de température extérieure (optionnel, pour l'anticipation météo et l'efficacité)
    CONF_INVERTER_MODE: True,     # Modulation continue de la consigne split (inverter)
    CONF_INVERTER_MIN_OFFSET: 0.5,  # °C au-dessus de la sonde split en maintien (point de fonctionnement optimal ~500 W)
    CONF_INVERTER_MAX_OFFSET: 2.0,  # °C au-dessus de la sonde split en chauffe rapide (évite les pics > 2 kW)
    CONF_INVERTER_P_BAND: 1.5,    # °C de bande proportionnelle pour l'offset inverter
    CONF_LEARNING_ENABLED: True,  # Apprentissage auto de l'inertie et du biais anti-yoyo
}
