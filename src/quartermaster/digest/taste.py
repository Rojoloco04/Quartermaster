"""What the owner likes, for deciding which listings are worth a line: the
Spotify and Last.fm top artists, and facts/interests.md (positives only)."""

from __future__ import annotations

import logging
import re

from ..config import Settings
from ..integrations import lastfm
from ..integrations.spotify import accounts as spotify_accounts, top_artist_names, top_artists

log = logging.getLogger(__name__)

_NOT_INTERESTED = re.compile(r"^#+\s*not interested\b.*?(?=^#+\s|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL)


def interests(settings: Settings) -> str:
    path = settings.facts_dir / "interests.md"
    return path.read_text(encoding="utf-8") if path.exists() else "(no facts/interests.md yet)"


def positive_interests(text: str) -> str:
    """interests.md minus its "Not interested" section. Naming a team there
    ("no Blues games") must not make that team's on-sale match."""
    return _NOT_INTERESTED.sub("", text)


def artist_names(settings: Settings) -> set[str]:
    """Spotify and Last.fm top artist names, lowercased. Each source fails on its own."""
    names: set[str] = set()
    labels = spotify_accounts(settings)
    if labels:
        try:
            names |= top_artist_names(settings, labels[0])
        except Exception as exc:  # noqa: BLE001 - the other source still works
            log.warning("taste: spotify unavailable: %s", exc)
    if settings.lastfm_api_key and settings.lastfm_user:
        try:
            names |= lastfm.artist_names(settings)
        except Exception as exc:  # noqa: BLE001 - the other source still works
            log.warning("taste: last.fm unavailable: %s", exc)
    return names


def top_artists_text(settings: Settings) -> str:
    """The same signal as prose, ranked, for the model picking events. Left
    out on failure: it's a filtering aid, not a digest section."""
    parts = []
    labels = spotify_accounts(settings)
    if labels:
        try:
            parts.append("Spotify, last ~6 months:\n" + top_artists(settings, labels[0]))
        except Exception as exc:  # noqa: BLE001
            log.warning("taste: spotify top artists unavailable: %s", exc)
    if settings.lastfm_api_key and settings.lastfm_user:
        try:
            parts.append("Last.fm, last 12 months:\n" + lastfm.summary(settings))
        except Exception as exc:  # noqa: BLE001
            log.warning("taste: last.fm top artists unavailable: %s", exc)
    return "\n\n".join(parts)


def matches(listing: dict, artists: set[str], interests_text: str) -> bool:
    """True if any act on the bill is a top artist or is named in interests.md
    (as a whole word, so "Tool" doesn't match "toolbox"). ``interests_text``
    is the lowercased positive part."""
    for act in listing.get("acts") or []:
        name = act.lower()
        if name in artists or re.search(rf"(?<!\w){re.escape(name)}(?!\w)", interests_text):
            return True
    return False
