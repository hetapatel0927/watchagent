"""
Unit tests for event detection logic.

These tests construct controlled sequences of readings to verify that the
detector fires exactly the events it should and remains silent when it shouldn't.
Each test is a direct expression of the design decisions documented in
app/event_detector.py and the README.

Approach
--------
- We write readings directly to the DB (bypassing the poller/HTTP layer).
- We call detect_events() with the new reading to test in isolation.
- We assert on which event_types appear in the returned list.
- We never call the real Open-Meteo API.
"""

import json
from datetime import datetime, timezone, timedelta

import pytest

from app.database import init_db, insert_reading, insert_event
from app.event_detector import (
    CITY_THRESHOLDS,
    RAPID_CHANGE_C,
    WIND_SEVERE_KMH,
    WIND_MODERATE_KMH,
    PRECIP_SEVERE_MM,
    PRECIP_MODERATE_MM,
    detect_events,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(db_path=path)
    return path


def _store(db: str, city: str, timestamp: str, **kwargs) -> tuple[dict, int]:
    """Build and store a reading, returning (reading_dict, reading_id)."""
    defaults = {
        "city": city,
        "timestamp": timestamp,
        "temperature_2m": 15.0,
        "apparent_temperature": 14.0,
        "precipitation": 0.0,
        "wind_speed_10m": 10.0,
        "weather_code": 0,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    reading = {**defaults, **kwargs}
    row_id = insert_reading(reading, db_path=db)
    assert row_id is not None, "Test setup: reading was rejected as duplicate"
    return reading, row_id


def _event_types(events: list[dict]) -> set[str]:
    return {e["event_type"] for e in events}


# ---------------------------------------------------------------------------
# Temperature extreme (city-calibrated thresholds)
# ---------------------------------------------------------------------------

class TestTemperatureExtreme:
    def test_ottawa_heat_fires_above_threshold(self, db):
        hot = CITY_THRESHOLDS["Ottawa"]["hot_temp"]
        reading, rid = _store(db, "Ottawa", "2024-07-01T14:00", temperature_2m=hot + 1)
        events = detect_events(reading, rid, db_path=db)
        assert "TEMPERATURE_EXTREME_HOT" in _event_types(events)

    def test_ottawa_heat_silent_below_threshold(self, db):
        hot = CITY_THRESHOLDS["Ottawa"]["hot_temp"]
        reading, rid = _store(db, "Ottawa", "2024-07-01T14:00", temperature_2m=hot - 2)
        events = detect_events(reading, rid, db_path=db)
        assert "TEMPERATURE_EXTREME_HOT" not in _event_types(events)

    def test_vancouver_heat_lower_threshold(self, db):
        """Vancouver fires at 28°C — a temperature that would NOT trigger Ottawa's 33°C threshold."""
        van_hot = CITY_THRESHOLDS["Vancouver"]["hot_temp"]
        ott_hot = CITY_THRESHOLDS["Ottawa"]["hot_temp"]
        assert van_hot < ott_hot, "Vancouver's heat threshold must be lower than Ottawa's"

        # 29°C: notable for Vancouver, not for Ottawa
        reading, rid = _store(db, "Vancouver", "2024-07-01T14:00", temperature_2m=van_hot + 1)
        events = detect_events(reading, rid, db_path=db)
        assert "TEMPERATURE_EXTREME_HOT" in _event_types(events)

    def test_ottawa_cold_fires_below_threshold(self, db):
        cold = CITY_THRESHOLDS["Ottawa"]["cold_temp"]
        reading, rid = _store(db, "Ottawa", "2024-01-15T08:00", temperature_2m=cold - 2)
        events = detect_events(reading, rid, db_path=db)
        assert "TEMPERATURE_EXTREME_COLD" in _event_types(events)

    def test_normal_ottawa_temperature_no_extreme_event(self, db):
        reading, rid = _store(db, "Ottawa", "2024-04-01T12:00", temperature_2m=18.0)
        events = detect_events(reading, rid, db_path=db)
        extreme_types = {e for e in _event_types(events) if "TEMPERATURE_EXTREME" in e}
        assert not extreme_types

    def test_apparent_temp_triggers_hot_even_if_actual_below_threshold(self, db):
        """Humidex (apparent_temp) can trigger the event independently of actual temp."""
        hot_apparent = CITY_THRESHOLDS["Ottawa"]["hot_apparent"]
        hot_actual = CITY_THRESHOLDS["Ottawa"]["hot_temp"]
        reading, rid = _store(
            db, "Ottawa", "2024-07-01T15:00",
            temperature_2m=hot_actual - 2,      # actual just below threshold
            apparent_temperature=hot_apparent + 1,  # apparent above threshold
        )
        events = detect_events(reading, rid, db_path=db)
        assert "TEMPERATURE_EXTREME_HOT" in _event_types(events)


# ---------------------------------------------------------------------------
# Rapid temperature change
# ---------------------------------------------------------------------------

class TestRapidTemperatureChange:
    def test_rapid_drop_fires_on_large_delta(self, db):
        _store(db, "Ottawa", "2024-01-01T10:00", temperature_2m=5.0)
        reading, rid = _store(
            db, "Ottawa", "2024-01-01T11:00",
            temperature_2m=5.0 - (RAPID_CHANGE_C + 1),  # drop exceeds threshold
        )
        events = detect_events(reading, rid, db_path=db)
        assert "RAPID_TEMPERATURE_DROP" in _event_types(events)

    def test_rapid_rise_fires_on_large_delta(self, db):
        _store(db, "Ottawa", "2024-05-01T06:00", temperature_2m=5.0)
        reading, rid = _store(
            db, "Ottawa", "2024-05-01T07:00",
            temperature_2m=5.0 + (RAPID_CHANGE_C + 1),
        )
        events = detect_events(reading, rid, db_path=db)
        assert "RAPID_TEMPERATURE_RISE" in _event_types(events)

    def test_small_change_does_not_fire(self, db):
        _store(db, "Ottawa", "2024-05-01T06:00", temperature_2m=15.0)
        reading, rid = _store(
            db, "Ottawa", "2024-05-01T07:00",
            temperature_2m=15.0 + (RAPID_CHANGE_C - 1),  # delta below threshold
        )
        events = detect_events(reading, rid, db_path=db)
        assert "RAPID_TEMPERATURE_RISE" not in _event_types(events)
        assert "RAPID_TEMPERATURE_DROP" not in _event_types(events)

    def test_first_reading_never_fires_rapid_change(self, db):
        """With only one reading there's no previous to compare against."""
        reading, rid = _store(db, "Ottawa", "2024-01-01T00:00", temperature_2m=-30.0)
        events = detect_events(reading, rid, db_path=db)
        rapid = {e for e in _event_types(events) if "RAPID_TEMPERATURE" in e}
        assert not rapid


# ---------------------------------------------------------------------------
# Freeze / thaw threshold crossing
# ---------------------------------------------------------------------------

class TestFreezeThaw:
    def test_freeze_crossing_fires_when_temp_drops_below_zero(self, db):
        _store(db, "Ottawa", "2024-11-15T21:00", temperature_2m=2.0)
        reading, rid = _store(db, "Ottawa", "2024-11-15T22:00", temperature_2m=-1.0)
        events = detect_events(reading, rid, db_path=db)
        assert "FREEZE_THRESHOLD_CROSSED" in _event_types(events)

    def test_thaw_crossing_fires_when_temp_rises_above_zero(self, db):
        _store(db, "Ottawa", "2024-03-10T09:00", temperature_2m=-2.0)
        reading, rid = _store(db, "Ottawa", "2024-03-10T10:00", temperature_2m=1.0)
        events = detect_events(reading, rid, db_path=db)
        assert "THAW_THRESHOLD_CROSSED" in _event_types(events)

    def test_no_crossing_when_both_readings_above_zero(self, db):
        _store(db, "Ottawa", "2024-07-01T10:00", temperature_2m=20.0)
        reading, rid = _store(db, "Ottawa", "2024-07-01T11:00", temperature_2m=21.0)
        events = detect_events(reading, rid, db_path=db)
        assert "FREEZE_THRESHOLD_CROSSED" not in _event_types(events)
        assert "THAW_THRESHOLD_CROSSED" not in _event_types(events)

    def test_no_crossing_when_both_readings_below_zero(self, db):
        _store(db, "Ottawa", "2024-01-01T10:00", temperature_2m=-10.0)
        reading, rid = _store(db, "Ottawa", "2024-01-01T11:00", temperature_2m=-12.0)
        events = detect_events(reading, rid, db_path=db)
        assert "FREEZE_THRESHOLD_CROSSED" not in _event_types(events)
        assert "THAW_THRESHOLD_CROSSED" not in _event_types(events)


# ---------------------------------------------------------------------------
# Precipitation
# ---------------------------------------------------------------------------

class TestPrecipitation:
    def test_heavy_precipitation_fires_above_severe_threshold(self, db):
        reading, rid = _store(
            db, "Vancouver", "2024-11-01T18:00",
            precipitation=PRECIP_SEVERE_MM + 2.0,
        )
        events = detect_events(reading, rid, db_path=db)
        assert "HEAVY_PRECIPITATION" in _event_types(events)

    def test_moderate_precipitation_fires_between_thresholds(self, db):
        reading, rid = _store(
            db, "Vancouver", "2024-11-01T18:00",
            precipitation=PRECIP_MODERATE_MM + 1.0,  # above moderate, below severe
        )
        events = detect_events(reading, rid, db_path=db)
        assert "MODERATE_PRECIPITATION" in _event_types(events)
        assert "HEAVY_PRECIPITATION" not in _event_types(events)

    def test_no_precipitation_event_when_dry(self, db):
        reading, rid = _store(db, "Vancouver", "2024-11-01T18:00", precipitation=0.0)
        events = detect_events(reading, rid, db_path=db)
        precip_events = {e for e in _event_types(events) if "PRECIPITATION" in e}
        assert not precip_events


# ---------------------------------------------------------------------------
# Wind
# ---------------------------------------------------------------------------

class TestWind:
    def test_severe_wind_fires_above_storm_threshold(self, db):
        reading, rid = _store(
            db, "Toronto", "2024-01-10T08:00",
            wind_speed_10m=WIND_SEVERE_KMH + 5.0,
        )
        events = detect_events(reading, rid, db_path=db)
        assert "STRONG_WIND_SEVERE" in _event_types(events)

    def test_moderate_wind_fires_between_thresholds(self, db):
        reading, rid = _store(
            db, "Toronto", "2024-01-10T08:00",
            wind_speed_10m=WIND_MODERATE_KMH + 5.0,
        )
        events = detect_events(reading, rid, db_path=db)
        assert "STRONG_WIND_MODERATE" in _event_types(events)
        assert "STRONG_WIND_SEVERE" not in _event_types(events)

    def test_light_wind_no_event(self, db):
        reading, rid = _store(db, "Toronto", "2024-01-10T08:00", wind_speed_10m=20.0)
        events = detect_events(reading, rid, db_path=db)
        wind_events = {e for e in _event_types(events) if "WIND" in e}
        assert not wind_events


# ---------------------------------------------------------------------------
# Severe WMO codes
# ---------------------------------------------------------------------------

class TestSevereWeatherCodes:
    def test_thunderstorm_code_fires(self, db):
        reading, rid = _store(db, "Ottawa", "2024-08-01T16:00", weather_code=95)
        events = detect_events(reading, rid, db_path=db)
        assert any("THUNDERSTORM" in e for e in _event_types(events))

    def test_freezing_rain_code_fires(self, db):
        reading, rid = _store(db, "Ottawa", "2024-11-20T10:00", weather_code=67)
        events = detect_events(reading, rid, db_path=db)
        assert any("FREEZING_RAIN" in e for e in _event_types(events))

    def test_clear_sky_code_no_weather_event(self, db):
        reading, rid = _store(db, "Ottawa", "2024-06-01T12:00", weather_code=0)
        events = detect_events(reading, rid, db_path=db)
        weather_events = {e for e in _event_types(events) if "SEVERE_WEATHER" in e}
        assert not weather_events


# ---------------------------------------------------------------------------
# Wind chill / heat index
# ---------------------------------------------------------------------------

class TestComfortIndex:
    def test_significant_wind_chill_fires_on_large_gap(self, db):
        reading, rid = _store(
            db, "Ottawa", "2024-01-15T09:00",
            temperature_2m=-10.0,
            apparent_temperature=-22.0,  # gap = -12°C (below -10 threshold)
        )
        events = detect_events(reading, rid, db_path=db)
        assert "SIGNIFICANT_WIND_CHILL" in _event_types(events)

    def test_significant_heat_index_fires_on_large_gap(self, db):
        reading, rid = _store(
            db, "Ottawa", "2024-07-15T14:00",
            temperature_2m=28.0,
            apparent_temperature=37.0,  # gap = +9°C (above 8 threshold)
        )
        events = detect_events(reading, rid, db_path=db)
        assert "SIGNIFICANT_HEAT_INDEX" in _event_types(events)


# ---------------------------------------------------------------------------
# Cooldown logic
# ---------------------------------------------------------------------------

class TestCooldown:
    def test_same_event_does_not_fire_twice_within_cooldown(self, db):
        """
        If TEMPERATURE_EXTREME_HOT fired recently, a second hot reading within
        the cooldown window must not produce another event.
        """
        hot = CITY_THRESHOLDS["Ottawa"]["hot_temp"] + 2

        # Store a prior event directly (simulating a previous fire 1 hour ago)
        past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        insert_event(
            {
                "city": "Ottawa",
                "event_type": "TEMPERATURE_EXTREME_HOT",
                "description": "Prior event",
                "severity": "high",
                "reading_id": None,
                "triggered_at": past_time,
                "details": "{}",
            },
            db_path=db,
        )

        reading, rid = _store(
            db, "Ottawa", "2024-07-01T15:00",
            temperature_2m=hot,
            apparent_temperature=hot,
        )
        events = detect_events(reading, rid, db_path=db)
        hot_events = [e for e in events if e["event_type"] == "TEMPERATURE_EXTREME_HOT"]
        assert len(hot_events) == 0, "Should not re-fire within cooldown window"

    def test_event_fires_again_after_cooldown_expires(self, db):
        """After the cooldown window passes, the same event type should fire again."""
        hot = CITY_THRESHOLDS["Ottawa"]["hot_temp"] + 2

        # Simulate an event that fired 4 hours ago (cooldown is 3 hours)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        insert_event(
            {
                "city": "Ottawa",
                "event_type": "TEMPERATURE_EXTREME_HOT",
                "description": "Old event",
                "severity": "high",
                "reading_id": None,
                "triggered_at": old_time,
                "details": "{}",
            },
            db_path=db,
        )

        reading, rid = _store(
            db, "Ottawa", "2024-07-01T16:00",
            temperature_2m=hot,
            apparent_temperature=hot,
        )
        events = detect_events(reading, rid, db_path=db)
        hot_events = [e for e in events if e["event_type"] == "TEMPERATURE_EXTREME_HOT"]
        assert len(hot_events) == 1, "Should fire again after cooldown expires"


# ---------------------------------------------------------------------------
# Normal conditions — no false positives
# ---------------------------------------------------------------------------

class TestNoFalsePositives:
    def test_calm_spring_day_produces_no_events(self, db):
        """A perfectly ordinary reading must produce zero events."""
        reading, rid = _store(
            db, "Ottawa", "2024-05-15T14:00",
            temperature_2m=18.0,
            apparent_temperature=17.5,
            precipitation=0.0,
            wind_speed_10m=12.0,
            weather_code=1,  # mainly clear
        )
        events = detect_events(reading, rid, db_path=db)
        assert events == [], f"Expected no events for calm reading, got: {_event_types(events)}"

    def test_event_details_contain_reason_field(self, db):
        """Every fired event must include a 'reason' in its details JSON."""
        hot = CITY_THRESHOLDS["Ottawa"]["hot_temp"] + 2
        reading, rid = _store(
            db, "Ottawa", "2024-07-01T14:00",
            temperature_2m=hot,
            apparent_temperature=hot,
        )
        events = detect_events(reading, rid, db_path=db)
        assert events, "Expected at least one event"
        for event in events:
            details = json.loads(event["details"])
            assert "reason" in details, f"Event {event['event_type']} missing 'reason' in details"

    def test_all_required_fields_present(self, db):
        """Every event dict must have all schema-required fields."""
        hot = CITY_THRESHOLDS["Ottawa"]["hot_temp"] + 2
        reading, rid = _store(
            db, "Ottawa", "2024-07-01T14:00",
            temperature_2m=hot,
            apparent_temperature=hot,
        )
        events = detect_events(reading, rid, db_path=db)
        required = {"city", "event_type", "description", "severity", "reading_id", "triggered_at", "details"}
        for event in events:
            missing = required - set(event.keys())
            assert not missing, f"Event missing fields: {missing}"
