import os

CITIES = [
    {"name": "Ottawa",    "lat": 45.42,  "lon": -75.69},
    {"name": "Toronto",   "lat": 43.70,  "lon": -79.42},
    {"name": "Vancouver", "lat": 49.25,  "lon": -123.12},
]

OPEN_METEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"

# How often the poller fetches data (seconds). Open-Meteo updates hourly,
# but we poll more frequently so we catch the new reading promptly.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))  # 15 min default
