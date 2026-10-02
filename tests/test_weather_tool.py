import httpx
import pytest

from resources.weather_tool import fetch_weather_conditions, weather_disruption_level

# Canned Open-Meteo style JSON response reused across tests (a test fixture value).
GOOD = {
    "current": {
        "time": "2026-10-01T09:00",
        "temperature_2m": 19.0,
        "wind_speed_10m": 4.7,
        "precipitation": 0.0,
        "weather_code": 0,
    }
}


# Helper: builds a real httpx client whose network layer is replaced by MockTransport. The
# handler function receives each request and returns a canned response, so no HTTP leaves the
# process (like WireMock/MockWebServer in Java, but in-memory).
def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# Proves: a good API response is parsed into the tool's result fields, and the request asked
# for the right fields (wind speed included). Why: the agent relies on these exact keys.
def test_success_parses_all_fields():
    # Dict mutated by the nested handler (a closure) to capture the request's query params,
    # since the handler's return value is the response, not a place to stash data.
    seen = {}

    # Closure: reads `seen` from the enclosing function. Records the query string, then replies 200.
    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=GOOD)

    result = fetch_weather_conditions(34.05, -118.24, client=client_for(handler))
    assert result["status"] == "ok"
    assert result["temperature_c"] == 19.0 and result["wind_speed_kmh"] == 4.7
    assert result["weather_disruption_level"] == "low"
    assert result["observed_at_timezone"] == "UTC"
    assert "wind_speed_10m" in seen["params"]["current"]


# Parametrized: latitude must be within +-90 and longitude within +-180, and must be numeric.
# Invalid values return status 'error' without ever calling the API.
@pytest.mark.parametrize("lat, lon", [(91, 0), (-91, 0), (0, 181), (0, -181), ("x", 0)])
# The handler raises if called, acting as a tripwire proving validation runs first.
def test_out_of_range_coordinates_are_errors(lat, lon):
    def handler(request):
        raise AssertionError("must not call the API")

    assert fetch_weather_conditions(lat, lon, client=client_for(handler))["status"] == "error"


# Proves: a persistent 500 gives status 'unavailable' after exactly one retry (2 calls).
def test_server_error_is_unavailable_after_retry():
    # List used as a call counter; the handler appends to it each time it is invoked.
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    result = fetch_weather_conditions(10, 10, client=client_for(handler))
    assert result["status"] == "unavailable" and len(calls) == 2


# Proves: a network timeout is reported as 'unavailable' rather than raising to the agent.
def test_timeout_is_unavailable():
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    assert fetch_weather_conditions(10, 10, client=client_for(handler))["status"] == "unavailable"


# Proves: HTTP 200 with an unexpected body (missing 'current') is handled as 'unavailable'.
# The inline lambda ignores the request (`r`) and returns a response with an empty JSON body.
def test_malformed_payload_is_unavailable():
    result = fetch_weather_conditions(10, 10, client=client_for(lambda r: httpx.Response(200, json={})))
    assert result["status"] == "unavailable"


# Proves: the retry works: first call 503, second call succeeds, so the result is ok.
# `iter([...])` makes a one-shot iterator and `next(responses)` pops the next canned response on
# each request, a compact way to script a sequence of replies.
def test_recovers_on_second_attempt():
    responses = iter([httpx.Response(503), httpx.Response(200, json=GOOD)])
    result = fetch_weather_conditions(10, 10, client=client_for(lambda r: next(responses)))
    assert result["status"] == "ok"


# Parametrized: (wind km/h, precipitation, weather code) -> expected disruption level. Covers
# the low/moderate/high thresholds, including each trigger (wind, rain, fog code, storm code).
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
