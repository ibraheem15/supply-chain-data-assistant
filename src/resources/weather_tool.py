"""fetch_weather_conditions: current weather at a point, from the Open-Meteo forecast API."""

# Lazy type hints so `X | None` works in annotations (see settings.py).
from __future__ import annotations

# `logging` is the stdlib logger (like java.util.logging/SLF4J). `Any` is a type hint meaning "any type".
# httpx is a third-party HTTP client library (like Apache HttpClient or OkHttp).
import logging
from typing import Any

import httpx

# Module-level logger with a named channel, so log lines can be filtered by name.
LOGGER = logging.getLogger("tools.weather")

# Configuration constants: the free Open-Meteo endpoint, which weather fields to request,
# the per-request timeout in seconds, and how many tries to make before giving up.
API_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = "temperature_2m,wind_speed_10m,precipitation,weather_code"
TIMEOUT_SECONDS = 10.0
ATTEMPTS = 2

# Thresholds for the disruption heuristic below. Named constants instead of magic numbers in the logic.
# weather_disruption_level is a labelled heuristic, not an operational measure.
HIGH_WIND_KMH = 60.0
MODERATE_WIND_KMH = 35.0
HIGH_PRECIP_MM = 10.0
MODERATE_PRECIP_MM = 2.5
# WMO codes: 0-3 clear/cloudy; 45+ fog, drizzle, rain, snow, showers; 95+ thunderstorm.
FOG_OR_PRECIP_CODE = 45
THUNDERSTORM_CODE = 95


# Turn raw weather numbers into a simple label: "high", "moderate" or "low".
# Inputs: wind speed (km/h), precipitation (mm), WMO weather code. Output: the label string.
# A pure function (no I/O), so it is trivial to unit test. Rules are checked from most to least severe.
def weather_disruption_level(wind_kmh: float, precipitation_mm: float, weather_code: int) -> str:
    if wind_kmh >= HIGH_WIND_KMH or precipitation_mm >= HIGH_PRECIP_MM or weather_code >= THUNDERSTORM_CODE:
        return "high"
    # Parentheses let one long condition span several lines; `or` short-circuits like Java's `||`.
    if (
        wind_kmh >= MODERATE_WIND_KMH
        or precipitation_mm >= MODERATE_PRECIP_MM
        or weather_code >= FOG_OR_PRECIP_CODE
    ):
        return "moderate"
    return "low"


# TOOL 2 of 3: get CURRENT weather at a latitude/longitude from Open-Meteo.
# Inputs: latitude, longitude, and an optional `client` (the bare `*` makes `client` keyword-only, so it
# must be passed as client=...). Tests pass a fake client here; this is dependency injection by parameter.
# Output: a dict {"status": "ok" | "error" | "unavailable", ...}. It never raises: the agent gets a
# readable message instead. "error" = bad input (agent can fix); "unavailable" = service failed.
# `httpx.Client | None` is a union type: either a Client or None (Java Optional-ish).
def fetch_weather_conditions(
    latitude: float, longitude: float, *, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Return current conditions at one point. Current only: not comparable to historical rows."""
    # Validate input. isinstance(x, int | float) accepts either type (older style: a tuple).
    # `-90 <= latitude <= 90` is a chained comparison, valid Python for a range check.
    if not (isinstance(latitude, int | float) and -90 <= latitude <= 90):
        return {"status": "error", "error": "latitude must be a number from -90 to 90."}
    if not (isinstance(longitude, int | float) and -180 <= longitude <= 180):
        return {"status": "error", "error": "longitude must be a number from -180 to 180."}

    # Query-string parameters; httpx URL-encodes them for us.
    params = {"latitude": latitude, "longitude": longitude, "current": CURRENT_FIELDS}
    # Remember whether WE created the client, so we only close one we created (a caller's client stays
    # open).
    owns_client = client is None
    # `a or b` returns a if it is truthy, else b: default to a fresh client when none was injected.
    client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
    # Holds the most recent failure text, reported if all attempts fail.
    last_error = "unknown error"
    # Outer try/finally guarantees the client is closed (like Java try-with-resources / finally).
    try:
        # Retry loop. range(1, n+1) yields 1..n; the end value is excluded.
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = client.get(API_URL, params=params)
                # Raises httpx.HTTPStatusError on a 4xx/5xx response, jumping to the except below.
                response.raise_for_status()
                # Parse the JSON body into a dict and take its "current" section (KeyError if missing).
                current = response.json()["current"]
                # float()/int() coerce values; a bad value raises ValueError/TypeError.
                wind = float(current["wind_speed_10m"])
                precipitation = float(current["precipitation"])
                code = int(current["weather_code"])
                # Success: returning inside the loop exits the function (and still runs the finally block).
                return {
                    "status": "ok",
                    "latitude": latitude,
                    "longitude": longitude,
                    "observed_at": current["time"],
                    "observed_at_timezone": "UTC",  # Open-Meteo defaults to GMT
                    "temperature_c": float(current["temperature_2m"]),
                    "wind_speed_kmh": wind,
                    "precipitation_mm": precipitation,
                    "weather_code": code,
                    "weather_disruption_level": weather_disruption_level(wind, precipitation, code),
                    "source": "open-meteo.com (current conditions, not historical)",
                }
            # Catch several exception types with one tuple. Network/HTTP problems, a missing JSON key and
            # bad values all
            # mean 'this attempt failed'. `as exc` binds the exception object. The loop then retries.
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                # f-string with expressions inside {}: type(exc).__name__ is the exception class name, e.g.
                # "ReadTimeout".
                last_error = f"{type(exc).__name__}: {exc}"
                # Logging uses %-style placeholders that are filled lazily, only if the message is actually
                # emitted.
                LOGGER.warning("Weather attempt %d/%d failed: %s", attempt, ATTEMPTS, last_error)
    # finally always runs, whether we returned, raised or fell through.
    finally:
        if owns_client:
            client.close()
    # Reached only when every attempt failed: report "unavailable" and tell the agent not to guess.
    return {
        "status": "unavailable",
        "error": f"Weather service did not return usable data ({last_error}). Do not estimate weather.",
    }
