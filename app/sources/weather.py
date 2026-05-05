"""Capital-city temperatures via Open-Meteo (free, no API key).

Source: https://open-meteo.com/ — proxies BOM data + ECMWF + GFS, returns clean
JSON, no auth required. Fair-use policy: <10k requests/day for non-commercial.
We hit it five times per 15-min refresh = 480 requests/day, well within limits.

Why this is here: temperature is the #1 driver of Australian gas demand
(winter heating) and a major driver of NEM electricity demand (summer
cooling). Reading temperature alongside the market data gives essential
context for "is today's load high because of weather, or because of an
outage?" """

from __future__ import annotations

import logging

import httpx


HEADERS = {"User-Agent": "markets-brief/1.0 (non-commercial)"}
ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# Capital cities in NEM-region order. Coordinates are city centroids; precision
# is fine because Open-Meteo's grid cells are ~10 km.
CITIES = [
    {"name": "Sydney",    "region": "NSW1", "lat": -33.87, "lon": 151.21, "tz": "Australia/Sydney"},
    {"name": "Melbourne", "region": "VIC1", "lat": -37.81, "lon": 144.96, "tz": "Australia/Melbourne"},
    {"name": "Brisbane",  "region": "QLD1", "lat": -27.47, "lon": 153.03, "tz": "Australia/Brisbane"},
    {"name": "Adelaide",  "region": "SA1",  "lat": -34.93, "lon": 138.60, "tz": "Australia/Adelaide"},
    {"name": "Perth",     "region": "SWIS", "lat": -31.95, "lon": 115.86, "tz": "Australia/Perth"},
]

log = logging.getLogger(__name__)


def fetch() -> dict:
    rows = []
    with httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
        for city in CITIES:
            try:
                rows.append(_fetch_city(client, city))
            except Exception as exc:
                log.warning("weather fetch failed for %s: %s", city["name"], exc)
                rows.append({
                    "name": city["name"],
                    "region": city["region"],
                    "current_c": None,
                    "today_max_c": None,
                    "tomorrow_max_c": None,
                    "tomorrow_min_c": None,
                    "stale": True,
                })
    return {"rows": rows}


def _fetch_city(client: httpx.Client, city: dict) -> dict:
    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "current": "temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": city["tz"],
        "forecast_days": 4,
    }
    resp = client.get(ENDPOINT, params=params)
    resp.raise_for_status()
    data = resp.json()
    current = data.get("current", {}).get("temperature_2m")
    daily = data.get("daily", {})
    maxes = daily.get("temperature_2m_max", []) or []
    mins = daily.get("temperature_2m_min", []) or []
    return {
        "name": city["name"],
        "region": city["region"],
        "current_c": round(float(current), 1) if current is not None else None,
        "today_max_c": round(float(maxes[0]), 1) if len(maxes) > 0 else None,
        "tomorrow_max_c": round(float(maxes[1]), 1) if len(maxes) > 1 else None,
        "tomorrow_min_c": round(float(mins[1]), 1) if len(mins) > 1 else None,
        "stale": False,
    }
