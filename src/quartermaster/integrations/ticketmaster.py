"""Ticketmaster Discovery API — events worth travelling to.

No account, no OAuth: one API key from developer.ticketmaster.com. The free
tier is 5,000 calls/day at 5 requests/second, deep paging capped at
``size * page < 1000`` — plenty for a handful of band/presale queries a day,
but the reason the events section is split into distance bands at all rather
than one 500-mile query (see ``events_for_bands``).

Ticketmaster's ``radius`` filter is a single upper bound, not a ring, so a
"60-250 mile" band still has to be queried at radius=250 and then filtered
down to the true band by distance computed from the venue's own lat/long.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, time as time_, timedelta, timezone

import httpx

from ..config import Settings

BASE = "https://app.ticketmaster.com/discovery/v2"
PAGE_SIZE = 200
MAX_DEEP_PAGE = 1000  # Discovery API hard cap: size * page must stay under this.

# The Discovery API's published rate limit is 5 requests/second.
_MIN_INTERVAL = 1 / 5
_last_call = 0.0

EARTH_RADIUS_MILES = 3958.7613


class TicketmasterError(RuntimeError):
    pass


def _throttle() -> None:
    global _last_call
    elapsed = time.monotonic() - _last_call
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_call = time.monotonic()


def _iso(value: datetime) -> str:
    """Ticketmaster wants UTC, no offset: ``2026-09-21T00:00:00Z``."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, a)))


def _normalize(raw: dict) -> dict:
    embedded = raw.get("_embedded") or {}
    venues = embedded.get("venues") or [{}]
    venue = venues[0] if venues else {}
    location = venue.get("location") or {}
    attractions = embedded.get("attractions") or []
    start = ((raw.get("dates") or {}).get("start") or {})
    onsale = (((raw.get("sales") or {}).get("public") or {})).get("startDateTime", "")

    lat = location.get("latitude")
    lon = location.get("longitude")
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "url": raw.get("url"),
        "starts_at": start.get("dateTime") or start.get("localDate", ""),
        "onsale_start": onsale,
        "venue_name": venue.get("name"),
        "city": (venue.get("city") or {}).get("name"),
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "attraction": attractions[0].get("name") if attractions else None,
    }


def search_events(
    settings: Settings,
    *,
    radius_miles: float,
    start: datetime | None = None,
    end: datetime | None = None,
    onsale_start: datetime | None = None,
    onsale_end: datetime | None = None,
) -> list[dict]:
    """One band's or one presale window's worth of events, fully paginated
    up to the API's deep-paging cap. Returns normalized event dicts."""
    settings.require("ticketmaster_api_key")
    params: dict[str, str] = {
        "apikey": settings.ticketmaster_api_key or "",
        "geoPoint": settings.prefs["home"]["geopoint"],
        "radius": str(int(radius_miles)),
        "unit": "miles",
        "size": str(PAGE_SIZE),
        "sort": "date,asc",
    }
    if start:
        params["startDateTime"] = _iso(start)
    if end:
        params["endDateTime"] = _iso(end)
    if onsale_start:
        params["onsaleStartDateTime"] = _iso(onsale_start)
    if onsale_end:
        params["onsaleEndDateTime"] = _iso(onsale_end)

    events: list[dict] = []
    page = 0
    while page * PAGE_SIZE < MAX_DEEP_PAGE:
        _throttle()
        resp = httpx.get(f"{BASE}/events.json", params={**params, "page": page}, timeout=15)
        if resp.status_code in (401, 403):
            raise TicketmasterError(
                f"Ticketmaster refused the request ({resp.status_code}). Check TICKETMASTER_API_KEY."
            )
        if resp.status_code == 404:
            # The API returns 404 for "no results", not an empty 200 - not an error.
            break
        if resp.status_code >= 400:
            raise TicketmasterError(f"Ticketmaster request failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        raw_events = (data.get("_embedded") or {}).get("events", [])
        events.extend(_normalize(e) for e in raw_events)

        total_pages = (data.get("page") or {}).get("totalPages", 1)
        page += 1
        if page >= total_pages:
            break

    return events


# A farther band's 1000-result deep-paging cap is still a lot of events to
# hand a model every run - a downtown metro over 60 days can fill it. Capped
# per band, keeping the soonest (the API already returns sort=date,asc and
# nothing here reorders), rather than trusting the model to silently ignore
# most of a multi-megabyte prompt.
MAX_EVENTS_PER_BAND = 60


def events_for_bands(settings: Settings, window_days: int) -> dict[str, list[dict]]:
    """Events in the next ``window_days``, bucketed into the configured
    distance bands by true distance from home, not by which query found them.

    Each band is queried at ``radius=band.max_miles`` (a closer band's events
    are also returned by a farther band's query - that's expected, not a bug)
    and then kept only if the computed distance actually falls in
    ``[min_miles, max_miles)``. An event id is only ever placed in one band's
    list, and each band's list is capped at ``MAX_EVENTS_PER_BAND``.
    """
    home = settings.prefs["home"]
    bands = sorted(settings.prefs["events"]["bands"], key=lambda b: b["max_miles"])
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=window_days)

    seen_ids: set[str] = set()
    result: dict[str, list[dict]] = {band["name"]: [] for band in bands}

    for band in bands:
        raw = search_events(settings, radius_miles=band["max_miles"], start=now, end=end)
        for event in raw:
            if event["id"] in seen_ids or event["lat"] is None or event["lon"] is None:
                continue
            distance = _haversine_miles(home["latitude"], home["longitude"], event["lat"], event["lon"])
            if not (band["min_miles"] <= distance < band["max_miles"]):
                continue
            seen_ids.add(event["id"])
            event["distance_miles"] = round(distance, 1)
            event["band"] = band["name"]
            event["bar"] = band["bar"]
            result[band["name"]].append(event)

    return {name: events[:MAX_EVENTS_PER_BAND] for name, events in result.items()}


def presales_starting(settings: Settings, on_date: date) -> list[dict]:
    """Events anywhere in range whose public onsale opens on ``on_date``.

    A Sunday digest is useless for tickets that sold out Thursday - this is
    meant to run daily, checked well before ticket windows tend to open.
    """
    bands = settings.prefs["events"]["bands"]
    radius = max((b["max_miles"] for b in bands), default=500)
    window_start = datetime.combine(on_date, time_.min, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)
    return search_events(settings, radius_miles=radius, onsale_start=window_start, onsale_end=window_end)


def item_id(event: dict, kind: str = "event") -> str:
    """Mute scheme: ``<kind>:artist/<name>/<id>``, so muting ``event:artist/Tool``
    silences every Tool show. ``kind`` is "event" for the digest line and
    "presale" for the presale ping - separate namespaces on purpose, so muting
    one ask never silently mutes the other."""
    if event.get("attraction"):
        return f"{kind}:artist/{event['attraction']}/{event['id']}"
    return f"{kind}:id/{event['id']}"


def format_event(event: dict) -> str:
    when = (event.get("starts_at") or "")[:10]
    where = ", ".join(p for p in (event.get("venue_name"), event.get("city")) if p)
    distance = f"  ({event['distance_miles']} mi)" if event.get("distance_miles") is not None else ""
    return f"{when}  {event.get('name')}  @ {where}{distance}  {event.get('url', '')}"
