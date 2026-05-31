"""
Background polling loop.

Fetches current weather from Open-Meteo for all three cities on a fixed interval.
Each response is deduplicated by (city, timestamp) before storage — the API updates
hourly, so multiple polls within the same hour will return the same timestamp and
will be silently dropped after the first successful insert.

Error handling contract (see .cursor/rules/error-handling.mdc):
  - On HTTP error: log WARNING with city, status, retry count; do not raise
  - On network error: log WARNING with city, error message, retry count; do not raise
  - After all retries: move on to the next city; one bad city must not stop the cycle
  - On event detection error: log ERROR with full traceback; continue
"""

import logging
import time
from datetime import datetime, timezone

import httpx

from app.config import CITIES, OPEN_METEO_BASE_URL, POLL_INTERVAL_SECONDS
from app.database import insert_event, insert_reading
from app.event_detector import detect_events

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 30


def fetch_city_weather(city: dict, client: httpx.Client) -> dict | None:
    """
    Fetch current conditions for one city.

    Returns a reading dict on success, None after all retries are exhausted.
    """
    name = city["name"]
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = client.get(
                OPEN_METEO_BASE_URL,
                params={
                    "latitude": city["lat"],
                    "longitude": city["lon"],
                    "current": (
                        "temperature_2m,apparent_temperature,"
                        "precipitation,wind_speed_10m,weather_code"
                    ),
                    "wind_speed_unit": "kmh",
                    "timezone": "auto",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            current = data["current"]
            return {
                "city": name,
                "timestamp": current["time"],
                "temperature_2m": current["temperature_2m"],
                "apparent_temperature": current["apparent_temperature"],
                "precipitation": current["precipitation"],
                "wind_speed_10m": current["wind_speed_10m"],
                "weather_code": current["weather_code"],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Fetch failed for city=%s http_status=%s attempt=%d/%d",
                name,
                exc.response.status_code,
                attempt,
                _MAX_RETRIES,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Fetch failed for city=%s error=%s attempt=%d/%d",
                name,
                str(exc),
                attempt,
                _MAX_RETRIES,
            )
        if attempt < _MAX_RETRIES:
            time.sleep(_RETRY_DELAY_SECONDS)

    logger.warning(
        "All retries exhausted for city=%s; skipping this poll cycle", name
    )
    return None


def poll_once(db_path: str | None = None) -> None:
    """Run one full poll cycle across all cities."""
    with httpx.Client() as client:
        for city in CITIES:
            name = city["name"]
            reading = fetch_city_weather(city, client)
            if reading is None:
                continue

            reading_id = insert_reading(reading, db_path=db_path)
            if reading_id is None:
                logger.debug(
                    "Duplicate skipped city=%s timestamp=%s",
                    name,
                    reading["timestamp"],
                )
                continue

            logger.info(
                "Reading stored city=%s timestamp=%s temp=%.1f°C wind=%.0f km/h precip=%.1f mm",
                name,
                reading["timestamp"],
                reading["temperature_2m"],
                reading["wind_speed_10m"],
                reading["precipitation"],
            )

            try:
                new_events = detect_events(reading, reading_id, db_path=db_path)
                for event in new_events:
                    insert_event(event, db_path=db_path)
                    logger.info(
                        "Event fired city=%s type=%s severity=%s description=%r",
                        event["city"],
                        event["event_type"],
                        event["severity"],
                        event["description"],
                    )
            except Exception:
                logger.exception(
                    "Event detection failed city=%s reading_id=%d", name, reading_id
                )


def run_poller(db_path: str | None = None) -> None:
    """Main polling loop. Runs until the process is killed."""
    logger.info("Poller started interval_seconds=%d", POLL_INTERVAL_SECONDS)
    while True:
        try:
            poll_once(db_path=db_path)
        except Exception:
            logger.exception("Unexpected error in poll cycle")
        time.sleep(POLL_INTERVAL_SECONDS)
