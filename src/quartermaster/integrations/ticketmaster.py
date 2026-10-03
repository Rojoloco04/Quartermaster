"""Ticketmaster Discovery API — events worth travelling to.

No account, no OAuth: one API key from developer.ticketmaster.com. The free
tier is 5,000 calls/day at 5 requests/second, deep paging capped at
``size * page < 1000`` (see ``search_events`` for how a month gets past it),
but the reason the events section is split into distance bands at all rather
than one 500-mile query (see ``events_for_bands``).

Ticketmaster's ``radius`` filter is a single upper bound, not a ring, so a
"60-250 mile" band still has to be queried at radius=250 and then filtered
down to the true band by distance computed from the venue's own lat/long.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone

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
    """A listing, in the fields ``db.listings`` stores and the digest's JSON
    uses, plus ``starts_at`` (UTC, for sorting) and ``lat``/``lon``."""
    embedded = raw.get("_embedded") or {}
    venues = embedded.get("venues") or [{}]
    venue = venues[0] if venues else {}
    location = venue.get("location") or {}
    attractions = embedded.get("attractions") or []
    start = ((raw.get("dates") or {}).get("start") or {})
    sales = raw.get("sales") or {}

    lat = location.get("latitude")
    lon = location.get("longitude")
    return {
        "id": raw.get("id"),
        "name": raw.get("name") or "",
        # Every billed act, headliner first - an on-sale for a show an artist
        # you follow is opening is still worth hearing about.
        "acts": [a["name"] for a in attractions if a.get("name")],
        "local_date": start.get("localDate", ""),
        "local_time": (start.get("localTime") or "")[:5],
        "starts_at": start.get("dateTime") or start.get("localDate", ""),
        "venue": venue.get("name"),
        "city": (venue.get("city") or {}).get("name"),
        "url": raw.get("url"),
        "onsale_at": (sales.get("public") or {}).get("startDateTime", ""),
        "presales": [{"name": p.get("name") or "Presale", "starts_at": p.get("startDateTime", "")}
                     for p in sales.get("presales") or [] if p.get("startDateTime")],
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
    }


def _get(params: dict, page: int) -> dict | None:
    """One page; None for "no results" (the API's 404, not an empty 200)."""
    _throttle()
    resp = httpx.get(f"{BASE}/events.json", params={**params, "page": page}, timeout=15)
    if resp.status_code in (401, 403):
        raise TicketmasterError(f"Ticketmaster refused the request ({resp.status_code}). Check TICKETMASTER_API_KEY.")
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise TicketmasterError(f"Ticketmaster request failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()


def search_events(
    settings: Settings,
    *,
    radius_miles: float,
    start: datetime | None = None,
    end: datetime | None = None,
    onsale_on: date | None = None,
    classification: str = "",
) -> list[dict]:
    """Every event matching, as normalized dicts.

    The API stops at ``MAX_DEEP_PAGE`` results, and 500 miles holds about that
    many events a day: a month-long query used to return only its first day.
    So a date window with more than that is split in half until each half
    fits. ``classification`` is ``classificationName`` (e.g. "music").

    ``onsale_on`` is ``onsaleOnStartDate``, a public on-sale opening that day.
    (``onsaleStartDateTime``, used until 2026-10, is silently ignored by the
    API: the "presale today" ping was really the soonest events in range,
    which is why the same show came back every morning.)"""
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
    if onsale_on:
        params["onsaleOnStartDate"] = onsale_on.isoformat()
    if classification:
        params["classificationName"] = classification

    data = _get(params, 0)
    if data is None:
        return []
    info = data.get("page") or {}
    if info.get("totalElements", 0) > MAX_DEEP_PAGE and start and end and end - start > timedelta(hours=12):
        mid = start + (end - start) / 2
        kw = {"radius_miles": radius_miles, "onsale_on": onsale_on, "classification": classification}
        return search_events(settings, start=start, end=mid, **kw) + search_events(settings, start=mid, end=end, **kw)

    events = [_normalize(e) for e in (data.get("_embedded") or {}).get("events", [])]
    total_pages = info.get("totalPages", 1)
    page = 1
    while page < total_pages and page * PAGE_SIZE < MAX_DEEP_PAGE:
        data = _get(params, page)
        if data is None:
            break
        events.extend(_normalize(e) for e in (data.get("_embedded") or {}).get("events", []))
        page += 1
    return events


def _distance(settings: Settings, event: dict) -> float | None:
    if event["lat"] is None or event["lon"] is None:
        return None
    home = settings.prefs["home"]
    return _haversine_miles(home["latitude"], home["longitude"], event["lat"], event["lon"])


def events_for_bands(settings: Settings, window_days: int) -> dict[str, list[dict]]:
    """Events in the next ``window_days``, bucketed into the configured
    distance bands by true distance from home, not by which query found them.

    Each band is queried at ``radius=band.max_miles`` (a closer band's events
    are also returned by a farther band's query - that's expected, not a bug)
    and then kept only if the computed distance actually falls in
    ``[min_miles, max_miles)``. An event id is only ever placed in one band's
    list. A band's optional ``classification`` narrows its query (the weekend
    band asks for music only). Not capped: the digest caps what it offers the
    model, after dropping what it offered before.
    """
    bands = sorted(settings.prefs["events"]["bands"], key=lambda b: b["max_miles"])
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=window_days)

    seen_ids: set[str] = set()
    result: dict[str, list[dict]] = {band["name"]: [] for band in bands}

    for band in bands:
        raw = search_events(settings, radius_miles=band["max_miles"], start=now, end=end,
                            classification=band.get("classification", ""))
        for event in raw:
            distance = _distance(settings, event)
            if event["id"] in seen_ids or distance is None:
                continue
            if not (band["min_miles"] <= distance < band["max_miles"]):
                continue
            seen_ids.add(event["id"])
            event["distance_miles"] = round(distance, 1)
            event["band"] = band["name"]
            event["bar"] = band["bar"]
            result[band["name"]].append(event)

    return {name: sorted(events, key=lambda e: e["starts_at"]) for name, events in result.items()}


def onsales_between(settings: Settings, first: date, days: int) -> list[dict]:
    """Events within the farthest band whose public on-sale opens on any day
    from ``first`` for ``days`` days. One query per day: ``onsaleOnStartDate``
    takes a single date, and a range query would hit the deep-paging cap."""
    radius = max((b["max_miles"] for b in settings.prefs["events"]["bands"]), default=500)
    found: dict[str, dict] = {}
    for offset in range(days):
        for event in search_events(settings, radius_miles=radius, onsale_on=first + timedelta(days=offset)):
            distance = _distance(settings, event)
            if event["id"] not in found and distance is not None and distance < radius:
                event["distance_miles"] = round(distance, 1)
                found[event["id"]] = event
    return sorted(found.values(), key=lambda e: e["onsale_at"])


def item_id(event: dict, kind: str = "event") -> str:
    """Mute scheme: ``<kind>:artist/<name>/<id>``, so muting ``event:artist/Tool``
    silences every Tool show. ``kind`` is "event" for the events section and
    "presale" for on-sale soon - separate namespaces on purpose, so muting one
    ask never silently mutes the other."""
    if event.get("acts"):
        return f"{kind}:artist/{event['acts'][0]}/{event['id']}"
    return f"{kind}:id/{event['id']}"
