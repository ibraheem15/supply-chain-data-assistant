import httpx

API_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = 10


def fetch_weather_conditions(latitude: float, longitude: float) -> dict:
    try:
        response = httpx.get(
            API_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,wind_speed_10m,precipitation",
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        current = response.json()["current"]
        return {
            "status": "ok",
            "temperature_c": current["temperature_2m"],
            "wind_speed_kmh": current["wind_speed_10m"],
            "precipitation_mm": current["precipitation"],
            "observed_at": current["time"],
        }

    except (httpx.HTTPError, KeyError) as e:
        return {"status": "error", "message": str(e)}



if __name__ == "__main__":
    print(fetch_weather_conditions(49.8, -113.1))  # near your data's coordinates
    print(fetch_weather_conditions(999, 999))  # invalid, the API rejects it
