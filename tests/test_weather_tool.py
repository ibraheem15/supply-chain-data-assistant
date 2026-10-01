import httpx
import pytest

from resources.weather_tool import fetch_weather_conditions, weather_disruption_level

GOOD = {
    "current": {
        "time": "2026-10-01T09:00",
        "temperature_2m": 19.0,
        "wind_speed_10m": 4.7,
        "precipitation": 0.0,
        "weather_code": 0,
    }
}


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_parses_all_fields():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=GOOD)

    result = fetch_weather_conditions(34.05, -118.24, client=client_for(handler))
    assert result["status"] == "ok"
    assert result["temperature_c"] == 19.0 and result["wind_speed_kmh"] == 4.7
    assert result["weather_disruption_level"] == "low"
    assert "wind_speed_10m" in seen["params"]["current"]


@pytest.mark.parametrize("lat, lon", [(91, 0), (-91, 0), (0, 181), (0, -181), ("x", 0)])
def test_out_of_range_coordinates_are_errors(lat, lon):
    def handler(request):
        raise AssertionError("must not call the API")

    assert fetch_weather_conditions(lat, lon, client=client_for(handler))["status"] == "error"


def test_server_error_is_unavailable_after_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    result = fetch_weather_conditions(10, 10, client=client_for(handler))
    assert result["status"] == "unavailable" and len(calls) == 2


def test_timeout_is_unavailable():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    assert fetch_weather_conditions(10, 10, client=client_for(handler))["status"] == "unavailable"


def test_malformed_payload_is_unavailable():
    result = fetch_weather_conditions(10, 10, client=client_for(lambda r: httpx.Response(200, json={})))
    assert result["status"] == "unavailable"


def test_recovers_on_second_attempt():
    responses = iter([httpx.Response(503), httpx.Response(200, json=GOOD)])
    result = fetch_weather_conditions(10, 10, client=client_for(lambda r: next(responses)))
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    "wind, precip, code, level",
    [
        (5, 0, 0, "low"),
        (40, 0, 0, "moderate"),
        (5, 3, 61, "moderate"),
        (5, 0, 45, "moderate"),
        (70, 0, 0, "high"),
        (5, 12, 65, "high"),
        (5, 0, 95, "high"),
    ],
)
def test_disruption_level(wind, precip, code, level):
    assert weather_disruption_level(wind, precip, code) == level
