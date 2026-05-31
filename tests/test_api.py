"""
API shape tests.

These tests verify that /health, /readings, and /events return the correct
structure, honour filtering, and handle edge cases.

The poller thread is mocked so tests are fully offline and deterministic.
"""

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.database import init_db, insert_reading, insert_event


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Provide a seeded database and a TestClient with the poller suppressed."""
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    init_db(db_path=db)

    # Seed three readings (two Ottawa, one Vancouver)
    readings = [
        {
            "city": "Ottawa",
            "timestamp": "2024-06-01T10:00",
            "temperature_2m": 22.0,
            "apparent_temperature": 21.0,
            "precipitation": 0.0,
            "wind_speed_10m": 15.0,
            "weather_code": 0,
            "fetched_at": "2024-06-01T10:05:00+00:00",
        },
        {
            "city": "Ottawa",
            "timestamp": "2024-06-01T11:00",
            "temperature_2m": 24.0,
            "apparent_temperature": 23.0,
            "precipitation": 0.0,
            "wind_speed_10m": 18.0,
            "weather_code": 0,
            "fetched_at": "2024-06-01T11:05:00+00:00",
        },
        {
            "city": "Vancouver",
            "timestamp": "2024-06-01T10:00",
            "temperature_2m": 17.0,
            "apparent_temperature": 16.0,
            "precipitation": 1.5,
            "wind_speed_10m": 22.0,
            "weather_code": 51,
            "fetched_at": "2024-06-01T10:05:00+00:00",
        },
    ]
    reading_ids = [insert_reading(r, db_path=db) for r in readings]

    # Seed one event
    insert_event(
        {
            "city": "Ottawa",
            "event_type": "TEMPERATURE_EXTREME_HOT",
            "description": "Ottawa: extreme heat 35.0°C",
            "severity": "high",
            "reading_id": reading_ids[0],
            "triggered_at": "2024-06-01T10:00:00+00:00",
            "details": '{"reason": "test"}',
        },
        db_path=db,
    )

    with patch("app.main.run_poller"):
        from app.main import app
        with TestClient(app) as client:
            yield client, db


class TestHealth:
    def test_status_ok(self, seeded):
        client, _ = seeded
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_correct_counts(self, seeded):
        client, _ = seeded
        data = resp = client.get("/health").json()
        assert data["readings_stored"] == 3
        assert data["events_stored"] == 1

    def test_all_required_keys_present(self, seeded):
        client, _ = seeded
        data = client.get("/health").json()
        assert set(data.keys()) == {"status", "readings_stored", "events_stored"}


class TestReadingsEndpoint:
    def test_returns_readings_key(self, seeded):
        client, _ = seeded
        data = client.get("/readings").json()
        assert "readings" in data
        assert isinstance(data["readings"], list)

    def test_default_limit_returns_all_seeded(self, seeded):
        client, _ = seeded
        data = client.get("/readings").json()
        assert len(data["readings"]) == 3

    def test_city_filter_returns_only_that_city(self, seeded):
        client, _ = seeded
        data = client.get("/readings?city=Ottawa").json()
        cities = {r["city"] for r in data["readings"]}
        assert cities == {"Ottawa"}
        assert len(data["readings"]) == 2

    def test_city_filter_vancouver(self, seeded):
        client, _ = seeded
        data = client.get("/readings?city=Vancouver").json()
        assert len(data["readings"]) == 1
        assert data["readings"][0]["city"] == "Vancouver"

    def test_limit_is_respected(self, seeded):
        client, _ = seeded
        data = client.get("/readings?limit=1").json()
        assert len(data["readings"]) == 1

    def test_reading_contains_required_fields(self, seeded):
        client, _ = seeded
        reading = client.get("/readings?limit=1").json()["readings"][0]
        required = {
            "id", "city", "timestamp", "temperature_2m", "apparent_temperature",
            "precipitation", "wind_speed_10m", "weather_code", "fetched_at",
        }
        assert required.issubset(set(reading.keys()))

    def test_unknown_city_returns_empty(self, seeded):
        client, _ = seeded
        data = client.get("/readings?city=Winnipeg").json()
        assert data["readings"] == []


class TestEventsEndpoint:
    def test_returns_events_key(self, seeded):
        client, _ = seeded
        data = client.get("/events").json()
        assert "events" in data
        assert isinstance(data["events"], list)

    def test_seeded_event_is_returned(self, seeded):
        client, _ = seeded
        data = client.get("/events").json()
        assert len(data["events"]) == 1

    def test_city_filter_matches_event(self, seeded):
        client, _ = seeded
        data = client.get("/events?city=Ottawa").json()
        assert len(data["events"]) == 1
        assert data["events"][0]["city"] == "Ottawa"

    def test_city_filter_no_match_returns_empty(self, seeded):
        client, _ = seeded
        data = client.get("/events?city=Vancouver").json()
        assert data["events"] == []

    def test_event_contains_required_fields(self, seeded):
        client, _ = seeded
        event = client.get("/events").json()["events"][0]
        required = {
            "id", "city", "event_type", "description", "severity",
            "reading_id", "triggered_at", "details",
        }
        assert required.issubset(set(event.keys()))
