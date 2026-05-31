# WatchAgent — Weather Monitor & AI Assistant

A production-grade service that monitors live weather across Ottawa, Toronto, and Vancouver, detects notable events using city-calibrated logic, and exposes the data through a REST API.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Docker Container                                           │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  FastAPI app  (uvicorn, port 8000)                   │  │
│  │                                                      │  │
│  │  GET /health     GET /readings    GET /events        │  │
│  │        │               │               │             │  │
│  │        └───────────────┴───────────────┘             │  │
│  │                        │                             │  │
│  │                   app/database.py                    │  │
│  │                        │                             │  │
│  │              ┌─────────▼──────────┐                 │  │
│  │              │   SQLite (WAL)     │ ◄── /data/       │  │
│  │              │  readings + events │     watchagent   │  │
│  │              └─────────▲──────────┘     .db          │  │
│  │                        │                             │  │
│  │              ┌─────────┴──────────┐                 │  │
│  │              │  app/poller.py     │  daemon thread   │  │
│  │              │  (every 15 min)    │                  │  │
│  │              └─────────┬──────────┘                 │  │
│  │                        │                             │  │
│  │              ┌─────────▼──────────┐                 │  │
│  │              │ event_detector.py  │                  │  │
│  │              │  10 event types    │                  │  │
│  │              └────────────────────┘                 │  │
│  └──────────────────────────────────────────────────────┘  │
│                           │                                 │
│               HTTP        │      Named volume               │
└───────────────────────────┼─────────────────────────────────┘
                            │
           ┌────────────────▼────────────────┐
           │  api.open-meteo.com             │
           │  Ottawa / Toronto / Vancouver   │
           └─────────────────────────────────┘
```

**Component responsibilities:**

- **`app/poller.py`** — Background thread. Fetches weather every 15 minutes, deduplicates by `(city, timestamp)`, triggers event detection on new readings. Retries failed fetches up to 3 times before moving on.
- **`app/database.py`** — Thread-safe SQLite layer. WAL mode for concurrent reads. The `UNIQUE(city, timestamp)` constraint is the deduplication guarantee.
- **`app/event_detector.py`** — Event detection logic. 10 categories, city-calibrated thresholds, cooldown windows to prevent alert fatigue.
- **`app/main.py`** — FastAPI app. Three endpoints. Starts the poller daemon thread on startup.

---

## Setup & Run

**Prerequisites:** Docker, Git.

```bash
git clone <your-repo-url>
cd watchagent
cp .env.example .env
docker compose up --build
```

The API is available at `http://localhost:8000` within seconds. The poller begins immediately — Ottawa, Toronto, and Vancouver readings appear on the first cycle.

Data persists in a named Docker volume (`watchagent_data`) across container restarts.

---

## API Reference

### `GET /health`

Service health check with storage counters.

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "readings_stored": 48,
  "events_stored": 3
}
```

---

### `GET /readings`

Return stored weather readings, newest first.

| Parameter | Type    | Default | Description              |
|-----------|---------|---------|--------------------------|
| `city`    | string  | —       | Filter by city name      |
| `limit`   | integer | 50      | Max results (1–1000)     |

```bash
# All cities, default limit
curl http://localhost:8000/readings

# Ottawa only, last 10
curl "http://localhost:8000/readings?city=Ottawa&limit=10"
```

```json
{
  "readings": [
    {
      "id": 42,
      "city": "Ottawa",
      "timestamp": "2024-07-01T14:00",
      "temperature_2m": 34.1,
      "apparent_temperature": 39.2,
      "precipitation": 0.0,
      "wind_speed_10m": 22.0,
      "weather_code": 1,
      "fetched_at": "2024-07-01T14:07:31.124+00:00"
    }
  ]
}
```

---

### `GET /events`

Return detected notable events, newest first.

| Parameter | Type    | Default | Description              |
|-----------|---------|---------|--------------------------|
| `city`    | string  | —       | Filter by city name      |
| `limit`   | integer | 50      | Max results (1–1000)     |

```bash
# All events
curl http://localhost:8000/events

# Vancouver events only
curl "http://localhost:8000/events?city=Vancouver&limit=5"
```

```json
{
  "events": [
    {
      "id": 7,
      "city": "Ottawa",
      "event_type": "TEMPERATURE_EXTREME_HOT",
      "description": "Ottawa: extreme heat 34.1°C (feels like 39.2°C)",
      "severity": "high",
      "reading_id": 42,
      "triggered_at": "2024-07-01T14:07:31.200+00:00",
      "details": "{\"temperature_2m\": 34.1, \"apparent_temperature\": 39.2, \"threshold_temp\": 33.0, \"threshold_apparent\": 38.0, \"reason\": \"City-calibrated heat threshold exceeded...\"}"
    }
  ]
}
```

---

## Running Tests

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest tests/ -v

# Run just event detection tests (most important)
pytest tests/test_event_detection.py -v
```

**51 tests, all passing.** Test coverage:
- `test_deduplication.py` — 6 tests: DB-level deduplication and end-to-end poller deduplication
- `test_event_detection.py` — 30 tests: every event category, both fire and no-fire cases, cooldown logic, schema validation
- `test_api.py` — 15 tests: all three endpoints, city filter, limit param, field presence

---

## Technology Choices

| Technology | Choice | Justification |
|------------|--------|---------------|
| **Web framework** | FastAPI | Native async, automatic OpenAPI docs, excellent Pydantic integration, easy TestClient for tests |
| **Database** | SQLite (WAL mode) | No separate process; persists with a named Docker volume; WAL mode allows concurrent API reads while the poller writes; correct for this data volume |
| **HTTP client** | httpx | Clean sync and async API; used synchronously in the poller thread |
| **Background work** | `threading.Thread(daemon=True)` | Shares the process, no IPC overhead; daemon=True means it exits cleanly with the container |
| **Language** | Python 3.11+ | Required; `X | None` union syntax throughout |

Flask was considered but rejected — it doesn't provide automatic validation or OpenAPI docs, and FastAPI's `lifespan` context manager is a cleaner way to manage the poller thread startup/shutdown than Flask's `before_first_request`.

---

## Event Detection Design

### The problem

A flat threshold ("fire if temp > 30°C") would fire constantly in Ottawa summers and never in Vancouver winters. Neither is useful. The goal is alerts that a human would find actionable — genuinely unusual conditions for that specific city.

### The approach

**Ten event categories, all with:**
1. **City-calibrated thresholds** — thresholds are derived from each city's climate baseline, not global constants
2. **Cooldown windows** — prevent repeated firing during sustained conditions (heat waves, storms)
3. **Two-tier severity** — `medium` for notable, `high` for dangerous
4. **Structured `details.reason`** — every event explains *why* it's notable, not just what the value was

### Event categories

| Category | Event Types | Threshold | Cooldown |
|----------|-------------|-----------|----------|
| **Temperature extreme** | `TEMPERATURE_EXTREME_HOT/COLD` | Per-city (Ottawa: 33°C, Vancouver: 28°C) | 3 hr |
| **Rapid change** | `RAPID_TEMPERATURE_RISE/DROP` | ≥6°C between consecutive readings | 2 hr |
| **Threshold crossing** | `FREEZE_THRESHOLD_CROSSED`, `THAW_THRESHOLD_CROSSED` | 0°C crossing | 6 hr |
| **Precipitation** | `HEAVY_PRECIPITATION`, `MODERATE_PRECIPITATION` | ≥10mm/hr (heavy), ≥5mm/hr (moderate) | 2/3 hr |
| **Wind** | `STRONG_WIND_SEVERE`, `STRONG_WIND_MODERATE` | ≥80 km/h (storm-force), ≥60 km/h (near-gale) | 2/3 hr |
| **Severe WMO codes** | `SEVERE_WEATHER_*` | WMO codes: 66-67 (freezing rain), 75-77 (heavy snow), 85-86 (snow showers), 95-99 (thunderstorm) | 3 hr |
| **Comfort indices** | `SIGNIFICANT_WIND_CHILL`, `SIGNIFICANT_HEAT_INDEX` | Apparent − actual ≤ −10°C or ≥ +8°C | 4 hr |
| **Inter-city anomaly** | `INTER_CITY_ANOMALY_WARMER/COLDER` | One city diverges ≥15°C from *both* others | 6 hr |

### City-specific calibration rationale

- **Ottawa** (continental): heat advisory at 33°C actual / 38°C humidex — these correspond to Environment Canada advisory thresholds. Cold at −25°C actual / −30°C apparent — Ottawa winters routinely reach −20°C so we reserve alerting for more extreme events.
- **Toronto** (similar, moderates by Lake Ontario): heat same as Ottawa, cold at −20/−25°C because it rarely matches Ottawa's extremes.
- **Vancouver** (oceanic): heat at 28°C because a heat dome event is genuinely unusual. Cold at −5°C because Vancouver's infrastructure (road treatment, heating capacity) is calibrated for mild winters — even a modest freeze is operationally significant.

### What does NOT fire

- Normal summer days (Ottawa 28°C, gentle wind, no precipitation)
- Minor precipitation (1–2 mm/hr — routine Vancouver drizzle)
- Repeated alerts during a sustained heat wave (cooldown prevents this)
- Divergence from only *one* other city (could be normal regional variation)

---

## Cursor Setup

### Rules (`.cursor/rules/`)

**`error-handling.mdc`** — Enforces a concrete logging contract for fetch failures. Rules: log at WARNING with `city=`, `http_status=`, `attempt=`/`max_retries` — exact format. Never raise in `fetch_city_weather`; return None. `logger.exception()` for event detection failures. This prevents silent failures and standardises how operators diagnose problems in the logs.

**`event-schema.mdc`** — Enforces the event dict shape. Every event must have all 7 required fields, `event_type` must be SCREAMING_SNAKE_CASE with underscore structure, `details` must be a JSON string with a `reason` key, cooldown must be checked via `_maybe_append()`. Temperature thresholds must be drawn from `CITY_THRESHOLDS[city]` — global flat thresholds are explicitly forbidden. Every new event type needs a fire test and a no-fire test.

### Agent (`.cursor/agents/`)

**`event-detection-reviewer.md`** — A specialist agent scoped to reviewing changes to `app/event_detector.py`. It has a 6-step checklist: threshold calibration, cooldown appropriateness, signal-to-noise (checked by running the data analysis skill), `details.reason` quality, test coverage, and schema compliance. The system prompt includes the full DB schema, the names of all 10 event categories, and the city-calibration rationale, so the agent doesn't hallucinate context. It can run `python .cursor/skills/analyze_data.py` and `pytest tests/test_event_detection.py` directly.

### Skill (`.cursor/skills/analyze_data.py`)

An executable Python script that queries the SQLite database and returns structured analysis. Supported questions:

```bash
# How often does each event type fire?
python .cursor/skills/analyze_data.py --question "event frequency by type"

# Min/max/avg temperature per city
python .cursor/skills/analyze_data.py --question "temperature trends per city"

# Scan for deduplication bugs
python .cursor/skills/analyze_data.py --question "deduplication check"

# Complete report (default)
python .cursor/skills/analyze_data.py

# Custom database path (e.g. for a local volume mount)
python .cursor/skills/analyze_data.py --db ./local.db --question "event rate"
```

The event frequency and event rate outputs are what the reviewer agent uses to assess whether a threshold is too sensitive (firing constantly) or too conservative (never firing in real data).

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_PATH` | `/data/watchagent.db` | SQLite database path |
| `POLL_INTERVAL_SECONDS` | `900` | Seconds between poll cycles (15 min) |

No API keys. Open-Meteo is free and unauthenticated.

---

## CI Pipeline

Two jobs run on every push to `main`:

1. **Test** — installs Python 3.11, installs requirements, runs `pytest tests/ -v`
2. **Build** — runs `docker build` to verify the image builds cleanly without credentials

Status is visible on the GitHub Actions tab of the repository.
