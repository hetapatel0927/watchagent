#!/usr/bin/env python3
"""
WatchAgent Replay Skill
========================
Replays the last N stored readings through the event detection logic and shows
which events would fire. Useful for validating threshold tuning without needing
to wait for live data.

Usage:
    python .cursor/skills/replay_events.py [N] [city]

Examples:
    python .cursor/skills/replay_events.py 50
    python .cursor/skills/replay_events.py 100 Ottawa
    python .cursor/skills/replay_events.py 24 Vancouver

Output: JSON list of {reading, events_fired} pairs.
"""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", "/data/watchagent.db")

# Add project root to path so we can import app.events
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from app.events import detect_events  # noqa: E402


def get_readings(city: str | None, n: int) -> list[dict]:
    if not Path(DB_PATH).exists():
        print(json.dumps({"error": f"Database not found at {DB_PATH}"}))
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if city:
        rows = conn.execute(
            "SELECT * FROM readings WHERE city=? ORDER BY timestamp DESC LIMIT ?",
            (city, n),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM readings ORDER BY timestamp DESC LIMIT ?",
            (n,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def replay(readings: list[dict]) -> list[dict]:
    """
    Walk through readings in chronological order, feeding each one through
    detect_events with the preceding readings as history. Mimics what the
    live poller does.
    """
    # Group by city, sort oldest-first for replay
    by_city: dict[str, list[dict]] = {}
    for r in readings:
        by_city.setdefault(r["city"], []).append(r)

    for city in by_city:
        by_city[city].sort(key=lambda x: x["timestamp"])

    results = []
    history_by_city: dict[str, list[dict]] = {}

    # Merge all cities in time order
    all_sorted = sorted(readings, key=lambda x: x["timestamp"])

    for reading in all_sorted:
        city = reading["city"]
        history = history_by_city.get(city, [])
        # newest-first for detect_events
        history_for_detection = [reading] + history

        events = await detect_events(
            city=city,
            reading_history=history_for_detection,
            reading_id=reading["id"],
        )
        if events:
            results.append({
                "reading": {
                    "city": reading["city"],
                    "timestamp": reading["timestamp"],
                    "temperature": reading["temperature"],
                    "wind_speed": reading["wind_speed"],
                    "precipitation": reading["precipitation"],
                    "apparent_temperature": reading["apparent_temperature"],
                },
                "events_fired": [
                    {
                        "event_type": e["event_type"],
                        "severity": e["severity"],
                        "description": e["description"],
                    }
                    for e in events
                ],
            })

        # Maintain history (newest-first, cap at 24)
        history_by_city[city] = ([reading] + history)[:24]

    return results


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    city = sys.argv[2] if len(sys.argv) > 2 else None

    readings = get_readings(city, n)
    if not readings:
        print(json.dumps({"error": "No readings found", "city": city, "n": n}))
        sys.exit(0)

    results = asyncio.run(replay(readings))

    output = {
        "readings_replayed": len(readings),
        "readings_that_fired_events": len(results),
        "city_filter": city,
        "events": results,
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
