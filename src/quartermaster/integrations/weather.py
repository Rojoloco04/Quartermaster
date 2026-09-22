"""The week's weather at home, for the digest.

Open-Meteo: free, no key, no account. One request for a 7-day daily forecast
at ``home.latitude``/``home.longitude``. Each day carries a ``rough`` flag set
here in code (storms, snow, a likely soaking, heat, cold, wind), so the model
never decides what counts as bad weather, only which rough days collide with
something on the calendar or an event worth mentioning.
"""

from __future__ import annotations

from datetime import date

import httpx

from ..config import Settings

API = "https://api.open-meteo.com/v1/forecast"
DAILY = ("weather_code", "temperature_2m_max", "temperature_2m_min",
         "precipitation_probability_max", "precipitation_sum", "wind_speed_10m_max")

# WMO weather interpretation codes, as Open-Meteo documents them.
WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "showers", 81: "heavy showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}
ROUGH_CODES = {56, 57, 65, 66, 67, 71, 73, 75, 77, 81, 82, 85, 86, 95, 96, 99}
RAIN_LIKELY = 60  # percent
HOT_F, COLD_F, WINDY_MPH = 95, 25, 25


class WeatherError(RuntimeError):
    pass


def rough_reasons(day: dict) -> list[str]:
    """Why a day is worth planning around; empty if it isn't."""
    reasons = []
    if day["code"] in ROUGH_CODES:
        reasons.append(day["summary"])
    elif (day["precip_chance"] or 0) >= RAIN_LIKELY:
        reasons.append(f"{day['precip_chance']}% chance of rain")
    if day["high"] is not None and day["high"] >= HOT_F:
        reasons.append(f"hot ({day['high']}°F)")
    if day["low"] is not None and day["low"] <= COLD_F:
        reasons.append(f"cold ({day['low']}°F)")
    if day["wind_mph"] is not None and day["wind_mph"] >= WINDY_MPH:
        reasons.append(f"windy ({day['wind_mph']} mph)")
    return reasons


def parse(data: dict) -> list[dict]:
    """Open-Meteo's column-per-variable ``daily`` block as one dict per day."""
    daily = data.get("daily") or {}
    days = []

    def col(name: str, i: int):
        values = daily.get(name) or []
        return values[i] if i < len(values) else None

    def whole(value):
        return None if value is None else round(value)

    for i, when in enumerate(daily.get("time") or []):
        code = col("weather_code", i)
        day = {
            "date": when,
            "weekday": date.fromisoformat(when).strftime("%a"),
            "code": code,
            "summary": WMO.get(code, "unknown"),
            "high": whole(col("temperature_2m_max", i)),
            "low": whole(col("temperature_2m_min", i)),
            "precip_chance": whole(col("precipitation_probability_max", i)),
            "precip_in": None if col("precipitation_sum", i) is None else round(col("precipitation_sum", i), 2),
            "wind_mph": whole(col("wind_speed_10m_max", i)),
        }
        day["rough"] = rough_reasons(day)
        days.append(day)
    return days


def forecast(settings: Settings, days: int = 7) -> list[dict]:
    """The next ``days`` days at home, today first."""
    home = settings.prefs["home"]
    try:
        resp = httpx.get(API, timeout=15, params={
            "latitude": home["latitude"], "longitude": home["longitude"], "daily": ",".join(DAILY),
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
            "timezone": "auto", "forecast_days": max(1, min(int(days), 16)),
        })
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherError(f"Open-Meteo request failed ({type(exc).__name__}).") from None
    if data.get("error"):
        raise WeatherError(f"Open-Meteo refused the request: {data.get('reason', 'no reason given')}")
    return parse(data)
