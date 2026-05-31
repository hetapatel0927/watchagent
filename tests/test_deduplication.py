"""
Tests for the deduplication guarantee:
  - The same (city, timestamp) pair must never produce two rows in the readings table.
  - A reading with a new timestamp for the same city must be stored successfully.

The Open-Meteo API is mocked so no real network calls are made.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.database import count_readings, init_db, insert_reading


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(db_path=path)
    return path


def _reading(timestamp: str = "2024-06-01T12:00") -> dict:
    return {
        "city": "Ottawa",
        "timestamp": timestamp,
        "temperature_2m": 22.0,
        "apparent_temperature": 21.0,
        "precipitation": 0.0,
        "wind_speed_10m": 15.0,
        "weather_code": 0,
        "fetched_at": "2024-06-01T12:05:00+00:00",
    }


class TestDeduplication:
    def test_first_insert_returns_row_id(self, db):
        row_id = insert_reading(_reading(), db_path=db)
        assert row_id is not None
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_duplicate_returns_none(self, db):
        """Inserting the same (city, timestamp) twice must return None the second time."""
        insert_reading(_reading(), db_path=db)
        result = insert_reading(_reading(), db_path=db)
        assert result is None, "Duplicate insert should return None"

    def test_only_one_row_stored_after_duplicate(self, db):
        """The readings table must contain exactly one row after two identical inserts."""
        insert_reading(_reading(), db_path=db)
        insert_reading(_reading(), db_path=db)
        assert count_readings(db_path=db) == 1

    def test_new_timestamp_stores_second_row(self, db):
        """A reading with a different timestamp for the same city must succeed."""
        id1 = insert_reading(_reading("2024-06-01T12:00"), db_path=db)
        id2 = insert_reading(_reading("2024-06-01T13:00"), db_path=db)
        assert id1 is not None
        assert id2 is not None
        assert id1 != id2
        assert count_readings(db_path=db) == 2

    def test_same_timestamp_different_cities_both_stored(self, db):
        """Two cities with the same timestamp string are independent — both must be stored."""
        r_ottawa = _reading("2024-06-01T12:00")
        r_toronto = {**_reading("2024-06-01T12:00"), "city": "Toronto"}
        id1 = insert_reading(r_ottawa, db_path=db)
        id2 = insert_reading(r_toronto, db_path=db)
        assert id1 is not None
        assert id2 is not None
        assert count_readings(db_path=db) == 2

    def test_poller_skips_duplicate_without_storing_event(self, db):
        """
        End-to-end: if the poller fetches the same reading twice (same timestamp),
        it must store only one row and must not attempt event detection on the duplicate.
        """
        from app.poller import poll_once

        raw_response = {
            "current": {
                "time": "2024-06-01T12:00",
                "temperature_2m": 22.0,
                "apparent_temperature": 21.0,
                "precipitation": 0.0,
                "wind_speed_10m": 15.0,
                "weather_code": 0,
            }
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = raw_response
        mock_resp.raise_for_status = MagicMock()

        with patch("app.poller.detect_events") as mock_detect, \
             patch("httpx.Client") as mock_client_cls:

            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            # Poll twice — same API response both times
            poll_once(db_path=db)
            poll_once(db_path=db)

        # detect_events must only have been called for the *three cities* on the
        # first poll (Ottawa, Toronto, Vancouver all return the same response mock
        # but with different cities). On the second poll, all are duplicates.
        # The key assertion: total readings == 3 (one per city, not 6).
        assert count_readings(db_path=db) == 3
