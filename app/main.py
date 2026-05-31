"""
FastAPI application entry point.

Chosen over Flask/Django because:
  - Native async support (future-proof for async DB adapters)
  - Automatic OpenAPI docs at /docs
  - Built-in request validation via Pydantic
  - Lifespan context manager for clean startup/shutdown

The poller runs in a daemon thread rather than a separate process so it shares
the same database path configuration as the API without any IPC complexity.
"""

import logging
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query

from app.database import count_events, count_readings, get_events, get_readings, init_db
from app.poller import run_poller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    thread = threading.Thread(target=run_poller, daemon=True, name="weather-poller")
    thread.start()
    logger.info("Weather poller thread started")
    yield
    logger.info("Shutting down WatchAgent")


app = FastAPI(
    title="WatchAgent",
    description="Weather monitor and event detector for Ottawa, Toronto, and Vancouver",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    """Service health check with storage counters."""
    return {
        "status": "ok",
        "readings_stored": count_readings(),
        "events_stored": count_events(),
    }


@app.get("/readings")
def readings(
    city: Optional[str] = Query(default=None, description="Filter by city name"),
    limit: int = Query(default=50, ge=1, le=1000, description="Max rows, newest first"),
):
    """Return stored weather readings, optionally filtered by city."""
    return {"readings": get_readings(city=city, limit=limit)}


@app.get("/events")
def events(
    city: Optional[str] = Query(default=None, description="Filter by city name"),
    limit: int = Query(default=50, ge=1, le=1000, description="Max rows, newest first"),
):
    """Return detected notable events, optionally filtered by city."""
    return {"events": get_events(city=city, limit=limit)}
