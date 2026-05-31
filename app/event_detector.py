"""
Event detection logic for WatchAgent.

Design philosophy
-----------------
Raw readings are not useful on their own. This module answers the question:
"is this reading worth waking someone up for?"

We define nine event categories. Each decision is calibrated to the city's
climate baseline, so the same temperature means different things in Vancouver
(oceanic, mild) vs Ottawa (continental, harsh). A flat 30°C threshold would
fire constantly in Ottawa summers and never in Vancouver winters; neither is
useful. Instead, thresholds are set relative to what is genuinely unusual for
each city.

Cooldown windows prevent the same event type from firing repeatedly while
conditions persist — an Ottawa heat wave shouldn't generate a new TEMPERATURE_
EXTREME_HOT every 15 minutes. The cooldown is calibrated per event type: fast-
moving hazards (heavy rain, severe wind) use shorter windows; slow-moving ones
(inter-city anomalies, freeze crossings) use longer ones.

Categories
----------
1. TEMPERATURE_EXTREME_HOT / _COLD   – city-calibrated heat/cold thresholds
2. RAPID_TEMPERATURE_RISE / _DROP    – ≥6°C change between consecutive readings
3. FREEZE_THRESHOLD_CROSSED          – temp goes positive → negative (icing risk)
4. THAW_THRESHOLD_CROSSED            – temp goes negative → positive (melt risk)
5. HEAVY_PRECIPITATION               – ≥10 mm/hr (flooding risk)
6. MODERATE_PRECIPITATION            – ≥5 mm/hr (significant rain/snow)
7. STRONG_WIND_SEVERE / _MODERATE    – storm-force (≥80) or near-gale (≥60 km/h)
8. SEVERE_WEATHER_*                  – dangerous WMO codes (thunderstorm, freezing rain, heavy snow)
9. SIGNIFICANT_WIND_CHILL            – apparent temp ≥10°C colder than actual
   SIGNIFICANT_HEAT_INDEX            – apparent temp ≥8°C hotter than actual
10. INTER_CITY_ANOMALY_WARMER/_COLDER – one city diverges >15°C from both others
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# City-specific temperature thresholds (°C).
# Set relative to each city's climate norms so we fire on genuinely unusual events.
# Ottawa:    continental climate; −20°C winters and 28°C summers are routine.
# Toronto:   similar but moderated by Lake Ontario; slightly milder both ways.
# Vancouver: oceanic; summers rarely exceed 25°C, winters rarely freeze hard.
CITY_THRESHOLDS: dict[str, dict] = {
    "Ottawa": {
        "hot_temp": 33.0,       # 28-30°C is a normal Ottawa summer day; 33+ is notable
        "hot_apparent": 38.0,   # Humidex 38+ is dangerous (Environment Canada advisory level)
        "cold_temp": -25.0,     # −20°C is a rough Ottawa winter night; −25 is notable
        "cold_apparent": -30.0, # Wind chill −30 is dangerous exposure territory
    },
    "Toronto": {
        "hot_temp": 33.0,
        "hot_apparent": 38.0,
        "cold_temp": -20.0,     # Toronto rarely reaches Ottawa's lows
        "cold_apparent": -25.0,
    },
    "Vancouver": {
        "hot_temp": 28.0,       # Vancouver above 28°C is genuinely unusual (heat dome territory)
        "hot_apparent": 32.0,
        "cold_temp": -5.0,      # Vancouver below −5°C is rare and operationally significant
        "cold_apparent": -8.0,
    },
}

# WMO codes that indicate dangerous conditions, mapped to (slug, label, severity).
# Reference: https://open-meteo.com/en/docs
SEVERE_WMO_CODES: dict[int, tuple[str, str, str]] = {
    66: ("FREEZING_DRIZZLE_HEAVY", "Heavy freezing drizzle",     "high"),
    67: ("FREEZING_RAIN_HEAVY",    "Heavy freezing rain",        "high"),
    75: ("HEAVY_SNOWFALL",         "Heavy snowfall",             "medium"),
    76: ("HEAVY_SNOWFALL",         "Heavy snowfall",             "medium"),
    77: ("SNOW_GRAINS_HEAVY",      "Heavy snow grains",          "medium"),
    85: ("SNOW_SHOWERS_HEAVY",     "Heavy snow showers",         "medium"),
    86: ("SNOW_SHOWERS_HEAVY",     "Heavy snow showers (heavy)", "high"),
    95: ("THUNDERSTORM",           "Thunderstorm",               "high"),
    96: ("THUNDERSTORM_HAIL",      "Thunderstorm with hail",     "high"),
    99: ("THUNDERSTORM_HAIL",      "Thunderstorm with heavy hail","high"),
}

WIND_MODERATE_KMH   = 60.0   # Near-gale: hazardous for cyclists and unsecured objects
WIND_SEVERE_KMH     = 80.0   # Storm-force: potential structural damage
PRECIP_MODERATE_MM  = 5.0    # Significant accumulation within the hour
PRECIP_SEVERE_MM    = 10.0   # Flooding risk
WIND_CHILL_DELTA    = -10.0  # apparent − actual ≤ this → significant wind chill
HEAT_INDEX_DELTA    = 8.0    # apparent − actual ≥ this → significant humidex
RAPID_CHANGE_C      = 6.0    # °C between consecutive hourly readings
INTER_CITY_DELTA    = 15.0   # °C above which one city is anomalously different


# ---------------------------------------------------------------------------
# Cooldown helpers
# ---------------------------------------------------------------------------

def _hours_since(iso_str: str) -> float:
    """Return how many hours have elapsed since an ISO-8601 datetime string."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _on_cooldown(
    city: str,
    event_type: str,
    cooldown_hours: float,
    db_path: str | None,
) -> bool:
    """Return True if this event type fired for this city within cooldown_hours."""
    from app.database import get_last_event_of_type

    last = get_last_event_of_type(city, event_type, db_path=db_path)
    if last is None:
        return False
    return _hours_since(last["triggered_at"]) < cooldown_hours


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_events(
    reading: dict,
    reading_id: int,
    db_path: str | None = None,
) -> list[dict]:
    """
    Analyse a freshly stored reading and return a list of event dicts.

    Each dict is ready to pass directly to ``database.insert_event()``.
    Returns an empty list when nothing notable is detected.
    """
    events: list[dict] = []
    city = reading["city"]
    temp = reading["temperature_2m"]
    apparent = reading["apparent_temperature"]
    precip = reading["precipitation"]
    wind = reading["wind_speed_10m"]
    code = reading["weather_code"]
    now = _now_iso()

    thresholds = CITY_THRESHOLDS.get(city, CITY_THRESHOLDS["Ottawa"])

    # 1 ── Temperature extremes (city-calibrated) ─────────────────────────
    if temp >= thresholds["hot_temp"] or apparent >= thresholds["hot_apparent"]:
        _maybe_append(
            events, city, "TEMPERATURE_EXTREME_HOT",
            f"{city}: extreme heat {temp:.1f}°C (feels like {apparent:.1f}°C)",
            "high" if apparent >= thresholds["hot_apparent"] else "medium",
            reading_id, now, db_path, cooldown_hours=3,
            details={
                "temperature_2m": temp,
                "apparent_temperature": apparent,
                "threshold_temp": thresholds["hot_temp"],
                "threshold_apparent": thresholds["hot_apparent"],
                "reason": (
                    "City-calibrated heat threshold exceeded. For Vancouver this fires at "
                    "28°C because such temperatures are genuinely unusual; Ottawa requires "
                    "33°C because summer heat is routine there."
                ),
            },
        )

    if temp <= thresholds["cold_temp"] or apparent <= thresholds["cold_apparent"]:
        _maybe_append(
            events, city, "TEMPERATURE_EXTREME_COLD",
            f"{city}: extreme cold {temp:.1f}°C (feels like {apparent:.1f}°C)",
            "high" if apparent <= thresholds["cold_apparent"] else "medium",
            reading_id, now, db_path, cooldown_hours=3,
            details={
                "temperature_2m": temp,
                "apparent_temperature": apparent,
                "threshold_temp": thresholds["cold_temp"],
                "threshold_apparent": thresholds["cold_apparent"],
                "reason": (
                    "City-calibrated cold threshold exceeded. Vancouver −5°C is rarer and "
                    "more disruptive than Ottawa −25°C, so thresholds reflect local norms."
                ),
            },
        )

    # 2 ── Rapid temperature change (consecutive readings) ────────────────
    from app.database import get_recent_readings_for_city

    history = get_recent_readings_for_city(city, n=2, db_path=db_path)
    if len(history) >= 2:
        prev_temp = history[1]["temperature_2m"]
        delta = temp - prev_temp
        if abs(delta) >= RAPID_CHANGE_C:
            direction = "RISE" if delta > 0 else "DROP"
            _maybe_append(
                events, city, f"RAPID_TEMPERATURE_{direction}",
                (
                    f"{city}: temperature {direction.lower()}d sharply "
                    f"{prev_temp:.1f}°C → {temp:.1f}°C ({delta:+.1f}°C)"
                ),
                "high" if abs(delta) >= 10.0 else "medium",
                reading_id, now, db_path, cooldown_hours=2,
                details={
                    "previous_temp": prev_temp,
                    "current_temp": temp,
                    "delta_c": round(delta, 2),
                    "threshold_c": RAPID_CHANGE_C,
                    "reason": (
                        f"A {abs(delta):.1f}°C change within one hour is unusual and "
                        "operationally significant — it indicates a rapid airmass change, "
                        "frontal passage, or data anomaly worth investigating."
                    ),
                },
            )

    # 3 ── Freeze / thaw threshold crossings ─────────────────────────────
        if history[1]["temperature_2m"] > 0 and temp <= 0:
            _maybe_append(
                events, city, "FREEZE_THRESHOLD_CROSSED",
                f"{city}: temperature crossed below freezing ({history[1]['temperature_2m']:.1f}°C → {temp:.1f}°C)",
                "medium",
                reading_id, now, db_path, cooldown_hours=6,
                details={
                    "previous_temp": history[1]["temperature_2m"],
                    "current_temp": temp,
                    "crossing": "freeze",
                    "reason": (
                        "Temperature dropped below 0°C — road surfaces, precipitation, and "
                        "standing water may now freeze. Operationally distinct from continued "
                        "sub-zero conditions."
                    ),
                },
            )
        elif history[1]["temperature_2m"] <= 0 and temp > 0:
            _maybe_append(
                events, city, "THAW_THRESHOLD_CROSSED",
                f"{city}: temperature crossed above freezing ({history[1]['temperature_2m']:.1f}°C → {temp:.1f}°C)",
                "low",
                reading_id, now, db_path, cooldown_hours=6,
                details={
                    "previous_temp": history[1]["temperature_2m"],
                    "current_temp": temp,
                    "crossing": "thaw",
                    "reason": (
                        "Temperature rose above 0°C — ice and snow may begin melting, "
                        "causing runoff and slippery conditions as melt occurs."
                    ),
                },
            )

    # 4 ── Precipitation intensity ────────────────────────────────────────
    if precip >= PRECIP_SEVERE_MM:
        _maybe_append(
            events, city, "HEAVY_PRECIPITATION",
            f"{city}: extreme precipitation {precip:.1f} mm/hr",
            "high",
            reading_id, now, db_path, cooldown_hours=2,
            details={
                "precipitation_mm": precip,
                "threshold_mm": PRECIP_SEVERE_MM,
                "reason": (
                    f"Precipitation ≥{PRECIP_SEVERE_MM} mm/hr is associated with flash "
                    "flooding risk, overwhelmed drainage systems, and rapid accumulation."
                ),
            },
        )
    elif precip >= PRECIP_MODERATE_MM:
        _maybe_append(
            events, city, "MODERATE_PRECIPITATION",
            f"{city}: significant precipitation {precip:.1f} mm/hr",
            "medium",
            reading_id, now, db_path, cooldown_hours=3,
            details={
                "precipitation_mm": precip,
                "threshold_mm": PRECIP_MODERATE_MM,
                "reason": (
                    f"Precipitation ≥{PRECIP_MODERATE_MM} mm/hr means meaningful accumulation "
                    "within the hour — relevant for commuters, outdoor operations, and snowpack."
                ),
            },
        )

    # 5 ── Wind speed ─────────────────────────────────────────────────────
    if wind >= WIND_SEVERE_KMH:
        _maybe_append(
            events, city, "STRONG_WIND_SEVERE",
            f"{city}: storm-force winds {wind:.0f} km/h",
            "high",
            reading_id, now, db_path, cooldown_hours=2,
            details={
                "wind_speed_kmh": wind,
                "threshold_kmh": WIND_SEVERE_KMH,
                "reason": (
                    f"≥{WIND_SEVERE_KMH:.0f} km/h is Beaufort scale 9+ (strong gale), "
                    "associated with structural damage, downed trees, and power outages."
                ),
            },
        )
    elif wind >= WIND_MODERATE_KMH:
        _maybe_append(
            events, city, "STRONG_WIND_MODERATE",
            f"{city}: strong winds {wind:.0f} km/h",
            "medium",
            reading_id, now, db_path, cooldown_hours=3,
            details={
                "wind_speed_kmh": wind,
                "threshold_kmh": WIND_MODERATE_KMH,
                "reason": (
                    f"≥{WIND_MODERATE_KMH:.0f} km/h is near-gale force — hazardous for "
                    "cyclists, pedestrians, and unsecured outdoor equipment."
                ),
            },
        )

    # 6 ── Severe WMO weather codes ───────────────────────────────────────
    if code in SEVERE_WMO_CODES:
        slug, label, severity = SEVERE_WMO_CODES[code]
        _maybe_append(
            events, city, f"SEVERE_WEATHER_{slug}",
            f"{city}: {label} (WMO {code})",
            severity,
            reading_id, now, db_path, cooldown_hours=3,
            details={
                "weather_code": code,
                "condition": label,
                "reason": (
                    f"WMO code {code} ({label}) represents a hazardous condition — "
                    "freezing precipitation, heavy snow, or thunderstorm activity."
                ),
            },
        )

    # 7 ── Wind chill / heat index ────────────────────────────────────────
    comfort_delta = apparent - temp

    if comfort_delta <= WIND_CHILL_DELTA:
        _maybe_append(
            events, city, "SIGNIFICANT_WIND_CHILL",
            (
                f"{city}: wind chill {abs(comfort_delta):.1f}°C colder than actual "
                f"({temp:.1f}°C → {apparent:.1f}°C)"
            ),
            "high" if comfort_delta <= -15.0 else "medium",
            reading_id, now, db_path, cooldown_hours=4,
            details={
                "temperature_2m": temp,
                "apparent_temperature": apparent,
                "wind_chill_effect_c": round(comfort_delta, 2),
                "wind_speed_kmh": wind,
                "threshold_c": WIND_CHILL_DELTA,
                "reason": (
                    "Large apparent-vs-actual gap driven by wind removes heat from exposed "
                    "skin much faster than the thermometer reading suggests."
                ),
            },
        )

    if comfort_delta >= HEAT_INDEX_DELTA:
        _maybe_append(
            events, city, "SIGNIFICANT_HEAT_INDEX",
            (
                f"{city}: humidex {comfort_delta:.1f}°C hotter than actual "
                f"({temp:.1f}°C → {apparent:.1f}°C)"
            ),
            "high" if comfort_delta >= 12.0 else "medium",
            reading_id, now, db_path, cooldown_hours=4,
            details={
                "temperature_2m": temp,
                "apparent_temperature": apparent,
                "heat_index_effect_c": round(comfort_delta, 2),
                "threshold_c": HEAT_INDEX_DELTA,
                "reason": (
                    "High humidity reduces the body's ability to cool through sweating, "
                    "making heat stress substantially worse than the air temperature implies."
                ),
            },
        )

    # 8 ── Inter-city temperature anomaly ────────────────────────────────
    _check_inter_city_anomaly(city, temp, reading_id, now, db_path, events)

    return events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _maybe_append(
    events: list,
    city: str,
    event_type: str,
    description: str,
    severity: str,
    reading_id: int,
    triggered_at: str,
    db_path: str | None,
    cooldown_hours: float,
    details: dict,
) -> None:
    """Append an event dict to `events` only if not on cooldown."""
    if _on_cooldown(city, event_type, cooldown_hours, db_path):
        return
    events.append(
        {
            "city": city,
            "event_type": event_type,
            "description": description,
            "severity": severity,
            "reading_id": reading_id,
            "triggered_at": triggered_at,
            "details": json.dumps(details),
        }
    )


def _check_inter_city_anomaly(
    city: str,
    temp: float,
    reading_id: int,
    now: str,
    db_path: str | None,
    events: list,
) -> None:
    """
    Fire an anomaly event if this city's temperature diverges from *both* other
    cities by INTER_CITY_DELTA or more.

    Rationale: divergence from a single other city might just be a microclimate
    difference or missing data. Diverging from both others simultaneously suggests
    something genuinely unusual — a localised weather system, sensor issue, or
    regional event worth flagging.
    """
    from app.database import get_recent_readings_for_city
    from app.config import CITIES

    others: dict[str, float] = {}
    for c in CITIES:
        if c["name"] == city:
            continue
        recent = get_recent_readings_for_city(c["name"], n=1, db_path=db_path)
        if recent:
            others[c["name"]] = recent[0]["temperature_2m"]

    if len(others) < 2:
        return  # Not enough comparison data yet

    diffs = {name: temp - other_temp for name, other_temp in others.items()}
    if not all(abs(d) >= INTER_CITY_DELTA for d in diffs.values()):
        return

    direction = "WARMER" if all(d > 0 for d in diffs.values()) else "COLDER"
    event_type = f"INTER_CITY_ANOMALY_{direction}"

    comparison = ", ".join(f"{n}: {t:.1f}°C" for n, t in others.items())
    _maybe_append(
        events, city, event_type,
        (
            f"{city} ({temp:.1f}°C) is dramatically {direction.lower()} than "
            f"both other cities ({comparison})"
        ),
        "medium",
        reading_id, now, db_path, cooldown_hours=6,
        details={
            "city_temp": temp,
            "other_cities": others,
            "differences_c": {n: round(d, 2) for n, d in diffs.items()},
            "threshold_c": INTER_CITY_DELTA,
            "reason": (
                f"{city} diverges ≥{INTER_CITY_DELTA}°C from both other monitored cities "
                "simultaneously — suggests a localised weather system or anomalous reading "
                "worth investigating."
            ),
        },
    )
