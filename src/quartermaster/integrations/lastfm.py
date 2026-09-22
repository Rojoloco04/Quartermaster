"""Last.fm, as a taste signal.

Spotify's "top artists" is a short, recent window over one app. Last.fm has the
whole scrobble history across every player, which is a better answer to "do I
actually like this artist" for the presale ping and the digest's events.

Public data, so no OAuth: an API key and a username. The key travels in the
query string, so no error message here ever includes the request URL.
"""

from __future__ import annotations

import httpx

from ..config import Settings

API = "https://ws.audioscrobbler.com/2.0/"
PERIODS = ("overall", "7day", "1month", "3month", "6month", "12month")

# An artist scrobbled a handful of times (a playlist shuffle, one curious
# listen) is not a taste signal worth pinging a presale over.
MIN_PLAYS = 5


class LastfmError(RuntimeError):
    pass


def top_artists(settings: Settings, period: str = "12month", limit: int = 100) -> list[tuple[str, int]]:
    """(artist, playcount), most-played first."""
    settings.require("lastfm_api_key", "lastfm_user")
    if period not in PERIODS:
        raise LastfmError(f"period must be one of {', '.join(PERIODS)}.")
    try:
        resp = httpx.get(API, timeout=15, params={
            "method": "user.gettopartists", "user": settings.lastfm_user,
            "api_key": settings.lastfm_api_key, "period": period,
            "limit": max(1, min(int(limit), 1000)), "format": "json",
        })
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LastfmError(f"Last.fm request failed ({type(exc).__name__}).") from None
    if "error" in data:
        raise LastfmError(f"Last.fm refused the request: {data.get('message', data['error'])}")
    artists = (data.get("topartists") or {}).get("artist") or []
    return [(a["name"], int(a.get("playcount") or 0)) for a in artists if a.get("name")]


def artist_names(settings: Settings) -> set[str]:
    """Lowercased names worth matching on: last year's and all-time top artists
    with at least ``MIN_PLAYS`` scrobbles."""
    names: set[str] = set()
    for period in ("12month", "overall"):
        names |= {name.lower() for name, plays in top_artists(settings, period, 200) if plays >= MIN_PLAYS}
    return names


def summary(settings: Settings, limit: int = 30) -> str:
    """Last year's top artists as digest context."""
    rows = top_artists(settings, "12month", limit)
    return "\n".join(f"- {name} ({plays} plays)" for name, plays in rows) or "No scrobbles in the last year."
