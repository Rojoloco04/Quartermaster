"""The digest's weather: parsing Open-Meteo, what counts as rough, and failing
into one "unavailable" line."""

import httpx
import pytest

from quartermaster import digest
from quartermaster.config import DEFAULTS, Settings
from quartermaster.integrations import weather

DATA = {
    "daily": {
        "time": ["2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29"],
        "weather_code": [3, 95, 61, 0],
        "temperature_2m_max": [81.4, 76.0, 70.2, 97.1],
        "temperature_2m_min": [66.0, 60.3, 22.0, 75.0],
        "precipitation_probability_max": [5, 80, 65, 0],
        "precipitation_sum": [0.0, 1.234, 0.4, 0.0],
        "wind_speed_10m_max": [8.2, 31.0, 9.0, 4.0],
    }
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(vault=tmp_path / "Vault", prefs=DEFAULTS)


def test_parse_one_dict_per_day():
    days = weather.parse(DATA)
    assert [d["weekday"] for d in days] == ["Sat", "Sun", "Mon", "Tue"]
    assert days[0] == {**days[0], "summary": "overcast", "high": 81, "low": 66, "precip_chance": 5, "rough": []}
    assert days[1]["precip_in"] == 1.23


def test_rough_is_decided_in_code():
    sat, sun, mon, tue = weather.parse(DATA)
    assert sat["rough"] == []
    assert sun["rough"] == ["thunderstorms", "windy (31 mph)"]
    assert mon["rough"] == ["65% chance of rain", "cold (22°F)"]
    assert tue["rough"] == ["hot (97°F)"]


def test_parse_tolerates_missing_columns():
    days = weather.parse({"daily": {"time": ["2026-09-26"], "weather_code": [1]}})
    assert days[0]["high"] is None and days[0]["rough"] == []


def test_forecast_asks_for_home_in_fahrenheit(settings, monkeypatch):
    seen = {}

    def fake_get(url, params, timeout):
        seen.update(params)
        return httpx.Response(200, json=DATA, request=httpx.Request("GET", url))

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    assert len(weather.forecast(settings)) == 4
    assert seen["latitude"] == DEFAULTS["home"]["latitude"]
    assert seen["temperature_unit"] == "fahrenheit" and seen["forecast_days"] == 7


def test_a_failure_is_one_unavailable_line(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(weather.httpx, "get", boom)
    assert digest._collect_weather(settings).startswith("unavailable: Open-Meteo request failed")


def test_the_digest_always_shows_the_week():
    assert "Always include a **Weather** section" in digest._prompt({})
