import os
import sqlite3
import threading
from contextlib import contextmanager

_lock = threading.Lock()


def _get_db_path(db_path: str | None = None) -> str:
    """Resolve database path: explicit arg > env var > default."""
    return db_path or os.getenv("DATABASE_PATH", "/data/watchagent.db")


def init_db(db_path: str | None = None) -> None:
    """Create tables if they don't already exist."""
    with get_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                city                 TEXT    NOT NULL,
                timestamp            TEXT    NOT NULL,
                temperature_2m       REAL    NOT NULL,
                apparent_temperature REAL    NOT NULL,
                precipitation        REAL    NOT NULL,
                wind_speed_10m       REAL    NOT NULL,
                weather_code         INTEGER NOT NULL,
                fetched_at           TEXT    NOT NULL,
                UNIQUE(city, timestamp)
            );

            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                city         TEXT    NOT NULL,
                event_type   TEXT    NOT NULL,
                description  TEXT    NOT NULL,
                severity     TEXT    NOT NULL,
                reading_id   INTEGER REFERENCES readings(id),
                triggered_at TEXT    NOT NULL,
                details      TEXT    NOT NULL
            );
            """
        )


@contextmanager
def get_connection(db_path: str | None = None):
    path = _get_db_path(db_path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_reading(reading: dict, db_path: str | None = None) -> int | None:
    """
    Persist a reading. Returns the new row id on success, None on duplicate
    (same city + timestamp already stored).
    """
    with _lock:
        with get_connection(db_path) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO readings
                        (city, timestamp, temperature_2m, apparent_temperature,
                         precipitation, wind_speed_10m, weather_code, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reading["city"],
                        reading["timestamp"],
                        reading["temperature_2m"],
                        reading["apparent_temperature"],
                        reading["precipitation"],
                        reading["wind_speed_10m"],
                        reading["weather_code"],
                        reading["fetched_at"],
                    ),
                )
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                return None


def insert_event(event: dict, db_path: str | None = None) -> None:
    """Persist a notable event."""
    with _lock:
        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO events
                    (city, event_type, description, severity,
                     reading_id, triggered_at, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["city"],
                    event["event_type"],
                    event["description"],
                    event["severity"],
                    event.get("reading_id"),
                    event["triggered_at"],
                    event["details"],
                ),
            )


def get_readings(
    city: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict]:
    with get_connection(db_path) as conn:
        if city:
            rows = conn.execute(
                "SELECT * FROM readings WHERE city = ? ORDER BY timestamp DESC LIMIT ?",
                (city, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM readings ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def get_events(
    city: str | None = None,
    limit: int = 50,
    db_path: str | None = None,
) -> list[dict]:
    with get_connection(db_path) as conn:
        if city:
            rows = conn.execute(
                "SELECT * FROM events WHERE city = ? ORDER BY triggered_at DESC LIMIT ?",
                (city, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY triggered_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def count_readings(db_path: str | None = None) -> int:
    with get_connection(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]


def count_events(db_path: str | None = None) -> int:
    with get_connection(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def get_recent_readings_for_city(
    city: str,
    n: int = 5,
    db_path: str | None = None,
) -> list[dict]:
    """Return the N most recent readings for a city, newest first."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM readings WHERE city = ? ORDER BY timestamp DESC LIMIT ?",
            (city, n),
        ).fetchall()
    return [dict(row) for row in rows]


def get_last_event_of_type(
    city: str,
    event_type: str,
    db_path: str | None = None,
) -> dict | None:
    """Return the most recent event of this type for this city, or None."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM events
            WHERE city = ? AND event_type = ?
            ORDER BY triggered_at DESC
            LIMIT 1
            """,
            (city, event_type),
        ).fetchone()
    return dict(row) if row else None
