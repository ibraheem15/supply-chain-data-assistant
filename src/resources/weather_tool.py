"""fetch_weather_conditions: current weather at a point, from the Open-Meteo forecast API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

LOGGER = logging.getLogger("tools.weather")

API_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = "temperature_2m,wind_speed_10m,precipitation,weather_code"
TIMEOUT_SECONDS = 10.0
ATTEMPTS = 2

# weather_disruption_level is a labelled heuristic, not an operational measure.
HIGH_WIND_KMH = 60.0
MODERATE_WIND_KMH = 35.0
HIGH_PRECIP_MM = 10.0
MODERATE_PRECIP_MM = 2.5
# WMO codes: 0-3 clear/cloudy; 45+ fog, drizzle, rain, snow, showers; 95+ thunderstorm.
FOG_OR_PRECIP_CODE = 45
THUNDERSTORM_CODE = 95


def weather_disruption_level(wind_kmh: float, precipitation_mm: float, weather_code: int) -> str:
    if wind_kmh >= HIGH_WIND_KMH or precipitation_mm >= HIGH_PRECIP_MM or weather_code >= THUNDERSTORM_CODE:
        return "high"
    if (
        wind_kmh >= MODERATE_WIND_KMH
        or precipitation_mm >= MODERATE_PRECIP_MM
        or weather_code >= FOG_OR_PRECIP_CODE
    ):
        return "moderate"
    return "low"


def fetch_weather_conditions(
    latitude: float, longitude: float, *, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Return current conditions at one point. Current only: not comparable to historical rows."""
    if not (isinstance(latitude, int | float) and -90 <= latitude <= 90):
        return {"status": "error", "error": "latitude must be a number from -90 to 90."}
    if not (isinstance(longitude, int | float) and -180 <= longitude <= 180):
        return {"status": "error", "error": "longitude must be a number from -180 to 180."}

    params = {"latitude": latitude, "longitude": longitude, "current": CURRENT_FIELDS}
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    last_error = "unknown error"
    try:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = client.get(API_URL, params=params)
                response.raise_for_status()
                current = response.json()["current"]
                wind = float(current["wind_speed_10m"])
                precipitation = float(current["precipitation"])
                code = int(current["weather_code"])
                return {
                    "status": "ok",
                    "latitude": latitude,
                    "longitude": longitude,
                    "observed_at": current["time"],
                    "temperature_c": float(current["temperature_2m"]),
                    "wind_speed_kmh": wind,
                    "precipitation_mm": precipitation,
                    "weather_code": code,
                    "weather_disruption_level": weather_disruption_level(wind, precipitation, code),
                    "source": "open-meteo.com (current conditions, not historical)",
                }
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("Weather attempt %d/%d failed: %s", attempt, ATTEMPTS, last_error)
    finally:
        if owns_client:
            client.close()
    return {
        "status": "unavailable",
        "error": f"Weather service did not return usable data ({last_error}). Do not estimate weather.",
    }
