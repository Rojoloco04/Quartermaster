"""What the owner likes, for deciding which listings are worth a line: the
Spotify and Last.fm top artists, and facts/interests.md (positives only)."""

from __future__ import annotations

import hashlib
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


# Genres too broad to say anything about taste, and words that turn up in
# ordinary prose: "Japanese pop culture" made every Pop on-sale a match.
_BROAD_GENRES = {"pop", "rock", "music", "other", "miscellaneous", "family", "undefined"}


def matches_genre(listing: dict, interests_text: str) -> bool:
    """True if Ticketmaster's genre or subgenre is named in interests.md: each
    "/"-part as a whole word, so "Hip-Hop/Rap" matches "rap" and
    "Dance/Electronic" matches "edm / electronic". A part inside a hyphenated
    word doesn't count ("k-pop"), nor does a ``_BROAD_GENRES`` one."""
    for genre in listing.get("genres") or []:
        for part in genre.split("/"):
            word = part.strip().lower()
            if len(word) > 2 and word not in _BROAD_GENRES and re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", interests_text):
                return True
    return False


def taste_key(interests_text: str) -> str:
    """A fingerprint of interests.md's positive part. An event the model passed
    over is offered again once this changes: a show judged before "esports" was
    added deserves a second look. Top artists aren't in it: they drift daily."""
    return hashlib.sha256(interests_text.strip().encode("utf-8")).hexdigest()[:12]
