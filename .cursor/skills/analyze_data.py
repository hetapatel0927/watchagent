#!/usr/bin/env python3
"""
WatchAgent data analysis skill.

Queries the SQLite database and returns structured analysis of stored readings
and events. Designed to be invoked by the Cursor agent as a tool.

Usage
-----
    python .cursor/skills/analyze_data.py [--question QUESTION] [--db PATH] [--json]

Supported questions
-------------------
    event frequency by type     — count of events grouped by event_type and city
    temperature trends per city — min / max / avg temperature per city
    readings per city           — total reading count per city
    recent events               — last 20 events across all cities
    deduplication check         — scan for any duplicate (city, timestamp) pairs
    precipitation analysis      — precipitation statistics per city
    wind analysis               — wind speed statistics per city
    event rate                  — ratio of events fired per reading stored
    summary                     — all of the above in one report (default)
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def connect(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"ERROR: Database not found at {db_path}", file=sys.stderr)
        print("Is the service running? Has it collected any data yet?", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def event_frequency_by_type(conn: sqlite3.Connection) -> dict:
    """Count events grouped by event_type, then by city within each type."""
    rows = conn.execute(
        """
        SELECT event_type, city, COUNT(*) as count
        FROM events
        GROUP BY event_type, city
        ORDER BY count DESC, event_type, city
        """
    ).fetchall()

    by_type: dict = {}
    for row in rows:
        et = row["event_type"]
        if et not in by_type:
            by_type[et] = {"total": 0, "by_city": {}}
        by_type[et]["by_city"][row["city"]] = row["count"]
        by_type[et]["total"] += row["count"]

    total_events = sum(v["total"] for v in by_type.values())
    return {
        "total_events": total_events,
        "event_types": len(by_type),
        "breakdown": by_type,
    }


def temperature_trends(conn: sqlite3.Connection) -> dict:
    """Min / max / avg temperature and apparent temperature per city."""
    rows = conn.execute(
        """
        SELECT
            city,
            COUNT(*)                            AS reading_count,
            ROUND(MIN(temperature_2m), 1)       AS temp_min,
            ROUND(MAX(temperature_2m), 1)       AS temp_max,
            ROUND(AVG(temperature_2m), 1)       AS temp_avg,
            ROUND(MIN(apparent_temperature), 1) AS apparent_min,
            ROUND(MAX(apparent_temperature), 1) AS apparent_max,
            ROUND(AVG(apparent_temperature), 1) AS apparent_avg,
            MIN(timestamp)                      AS first_reading,
            MAX(timestamp)                      AS last_reading
        FROM readings
        GROUP BY city
        ORDER BY city
        """
    ).fetchall()
    return {"cities": [dict(r) for r in rows]}


def readings_per_city(conn: sqlite3.Connection) -> dict:
    """Total reading count and time span per city."""
    rows = conn.execute(
        """
        SELECT
            city,
            COUNT(*)       AS total_readings,
            MIN(timestamp) AS earliest,
            MAX(timestamp) AS latest
        FROM readings
        GROUP BY city
        ORDER BY city
        """
    ).fetchall()
    return {"cities": [dict(r) for r in rows]}


def recent_events(conn: sqlite3.Connection, n: int = 20) -> dict:
    """Last N events across all cities."""
    rows = conn.execute(
        """
        SELECT id, city, event_type, severity, description, triggered_at
        FROM events
        ORDER BY triggered_at DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    return {"recent_events": [dict(r) for r in rows], "count": len(rows)}


def deduplication_check(conn: sqlite3.Connection) -> dict:
    """Scan for duplicate (city, timestamp) pairs — there should be none."""
    dupes = conn.execute(
        """
        SELECT city, timestamp, COUNT(*) AS occurrences
        FROM readings
        GROUP BY city, timestamp
        HAVING COUNT(*) > 1
        ORDER BY occurrences DESC
        """
    ).fetchall()

    total_readings = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    return {
        "total_readings": total_readings,
        "duplicates_found": len(dupes),
        "duplicate_pairs": [dict(r) for r in dupes],
        "status": "PASS" if not dupes else "FAIL",
    }


def precipitation_analysis(conn: sqlite3.Connection) -> dict:
    """Precipitation statistics per city."""
    rows = conn.execute(
        """
        SELECT
            city,
            COUNT(*)                         AS reading_count,
            ROUND(MAX(precipitation), 2)     AS precip_max_mm,
            ROUND(AVG(precipitation), 2)     AS precip_avg_mm,
            SUM(CASE WHEN precipitation > 0 THEN 1 ELSE 0 END) AS readings_with_precip,
            SUM(CASE WHEN precipitation >= 5  THEN 1 ELSE 0 END) AS readings_moderate_plus,
            SUM(CASE WHEN precipitation >= 10 THEN 1 ELSE 0 END) AS readings_severe
        FROM readings
        GROUP BY city
        ORDER BY city
        """
    ).fetchall()
    return {"cities": [dict(r) for r in rows]}


def wind_analysis(conn: sqlite3.Connection) -> dict:
    """Wind speed statistics per city."""
    rows = conn.execute(
        """
        SELECT
            city,
            COUNT(*)                              AS reading_count,
            ROUND(MAX(wind_speed_10m), 1)         AS wind_max_kmh,
            ROUND(AVG(wind_speed_10m), 1)         AS wind_avg_kmh,
            SUM(CASE WHEN wind_speed_10m >= 60 THEN 1 ELSE 0 END) AS readings_near_gale,
            SUM(CASE WHEN wind_speed_10m >= 80 THEN 1 ELSE 0 END) AS readings_storm_force
        FROM readings
        GROUP BY city
        ORDER BY city
        """
    ).fetchall()
    return {"cities": [dict(r) for r in rows]}


def event_rate(conn: sqlite3.Connection) -> dict:
    """Events fired per reading stored — overall and per city."""
    total_readings = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    per_city = conn.execute(
        """
        SELECT
            r.city,
            COUNT(DISTINCT r.id)  AS readings,
            COUNT(DISTINCT e.id)  AS events
        FROM readings r
        LEFT JOIN events e ON e.city = r.city
        GROUP BY r.city
        ORDER BY r.city
        """
    ).fetchall()

    return {
        "overall": {
            "total_readings": total_readings,
            "total_events": total_events,
            "events_per_reading": round(total_events / total_readings, 4) if total_readings else 0,
        },
        "per_city": [
            {
                "city": row["city"],
                "readings": row["readings"],
                "events": row["events"],
                "events_per_reading": round(row["events"] / row["readings"], 4) if row["readings"] else 0,
            }
            for row in per_city
        ],
    }


def full_summary(conn: sqlite3.Connection) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "readings_per_city": readings_per_city(conn),
        "temperature_trends": temperature_trends(conn),
        "precipitation_analysis": precipitation_analysis(conn),
        "wind_analysis": wind_analysis(conn),
        "event_frequency": event_frequency_by_type(conn),
        "event_rate": event_rate(conn),
        "deduplication_check": deduplication_check(conn),
        "recent_events": recent_events(conn, n=10),
    }


QUESTION_MAP = {
    "event frequency by type":     event_frequency_by_type,
    "temperature trends per city":  temperature_trends,
    "readings per city":            readings_per_city,
    "recent events":                recent_events,
    "deduplication check":          deduplication_check,
    "precipitation analysis":       precipitation_analysis,
    "wind analysis":                wind_analysis,
    "event rate":                   event_rate,
    "summary":                      full_summary,
}


def _print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        # Pretty human-readable output
        print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--question", "-q",
        default="summary",
        help="Analysis to run (default: summary). See supported questions above.",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("DATABASE_PATH", "/data/watchagent.db"),
        help="Path to the SQLite database (default: $DATABASE_PATH or /data/watchagent.db)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON (default: pretty-printed JSON)",
    )
    args = parser.parse_args()

    # Normalise question
    q = args.question.strip().lower()
    handler = None
    for key, fn in QUESTION_MAP.items():
        if q == key or q.startswith(key[:6]):
            handler = fn
            break

    if handler is None:
        print(f"Unknown question: {args.question!r}", file=sys.stderr)
        print("Supported questions:", file=sys.stderr)
        for key in QUESTION_MAP:
            print(f"  - {key}", file=sys.stderr)
        sys.exit(1)

    conn = connect(args.db)
    try:
        result = handler(conn)
    finally:
        conn.close()

    _print_result(result, as_json=args.json)


if __name__ == "__main__":
    main()
