"""The weekly digest, and its daily presale sibling.

Collectors are plain Python and do no reasoning: each one fetches, normalises
and hands the orchestrator a plain list. Everything nudge-able is filtered
through the mute list before it ever reaches a prompt. One Agent SDK call
(``agent.digest_profile``) then writes the prose - the model is never asked
to decide what's true, only how to say what the collectors already decided
was true.

``record`` distinguishes a real run from ``--dry-run``: a dry run collects
real data (so the preview is honest) but never marks anything as "shown" to
the owner and never sends or archives anything. Price and event *observation*
logs (price_history, events_seen) are written either way - those are records
of a check having happened, independent of whether the owner was ever told
about it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import agent, db, mutes, stale
from .config import Settings
from .integrations import lastfm, prices, ticketmaster
from .integrations.google import list_events
from .integrations.spotify import accounts as spotify_accounts, top_artist_names, top_artists
from .surfaces.digest_send import send_dm

log = logging.getLogger(__name__)

_PROMPT_RULES = """\
You are writing Quartermaster's weekly digest as a single Discord DM. \
Everything below is already-verified structured data from plain-Python \
collectors - never invent a number, date, price, or event that isn't in it.

Formatting:
- Use **bold** for section labels, never # headings - Discord doesn't render them.
- Skip a section entirely (no heading, no "nothing to report") if it has no items.
- Keep it scannable: short lines, not paragraphs.
- For events, respect each band's bar (low = a casual local mention is fine, \
medium = should be a genuine interest match, high = must clearly be worth a \
trip) using the interests and top-artists context to judge what's worth \
including - don't just dump every event you're handed.
- If an item's times_shown is 2 or more, it's been mentioned before; vary the \
phrasing rather than repeating the same line every time.
- If a section's data is the string "unavailable: ...", mention briefly that \
it couldn't be checked rather than omitting it silently.
- If wishlist price checks failed for some items, mention that briefly too - \
"couldn't check X" - never imply "no change" for something that failed.
"""


def _prompt(payload: dict) -> str:
    return _PROMPT_RULES + "\n\nData:\n" + json.dumps(payload, indent=2, default=str)


# --- Collectors -------------------------------------------------------------------
#
# Each collector's failure degrades only its own section. They catch Exception,
# not just RuntimeError: googleapiclient's HttpError, SpotifyException and a
# plain httpx network error are none of them RuntimeErrors, and one flaky API
# must not cost the whole digest.


def _unavailable(section: str, exc: Exception) -> str:
    log.warning("digest: %s unavailable: %s", section, exc)
    return f"unavailable: {exc}"


def _mark(conn: sqlite3.Connection, iid: str, kind: str, summary: str, record: bool) -> int:
    """Times shown so far, counting this one only on a real (non-dry) run."""
    return db.record_surfaced(conn, iid, kind, summary) if record else db.shown_count(conn, iid)


def _collect_calendar(settings: Settings) -> str:
    days = settings.prefs["digest"]["calendar_days_ahead"]
    today = date.today()
    try:
        return list_events(settings, today.isoformat(), (today + timedelta(days=days)).isoformat())
    except Exception as exc:  # noqa: BLE001 - see above
        return _unavailable("calendar", exc)


def _collect_events(settings: Settings, conn: sqlite3.Connection, mute_list, record: bool) -> dict:
    try:
        banded = ticketmaster.events_for_bands(settings, settings.prefs["events"]["window_days"])
    except Exception as exc:  # noqa: BLE001 - see above
        return {"unavailable": _unavailable("events", exc)}

    out: dict[str, object] = {}
    for band_name, events in banded.items():
        kept = []
        for event in events:
            iid = ticketmaster.item_id(event)
            if mutes.is_muted(iid, mute_list):
                continue
            db.record_event_seen(
                conn, event["id"], event["name"], event["starts_at"], band_name, event.get("venue_name") or ""
            )
            kept.append({**event, "times_shown": _mark(conn, iid, "event", event["name"], record)})
        out[band_name] = kept
    return out


def _collect_prices(settings: Settings, conn: sqlite3.Connection, mute_list, record: bool) -> dict:
    try:
        result = prices.check_all(settings, conn)
    except Exception as exc:  # noqa: BLE001 - see above
        return {"unavailable": _unavailable("prices", exc)}

    drops = []
    for drop in result["drops"]:
        iid = f"price:{drop['block_id']}"
        if not mutes.is_muted(iid, mute_list):
            drops.append({**drop, "times_shown": _mark(conn, iid, "price", drop["item_name"], record)})
    return {"drops": drops, "failures": result["failures"]}


def _collect_stale(settings: Settings, conn: sqlite3.Connection, mute_list, record: bool) -> list[dict]:
    found = []
    for page in stale.find_stale_pages(settings, conn):
        iid = stale.item_id(page["page_id"])
        if not mutes.is_muted(iid, mute_list):
            found.append({**page, "times_shown": _mark(conn, iid, "stale", page["title"] or "", record)})
    return found


def _interests(settings: Settings) -> str:
    path = settings.facts_dir / "interests.md"
    return path.read_text(encoding="utf-8") if path.exists() else "(no facts/interests.md yet)"


def _top_artists(settings: Settings) -> str:
    """Spotify (recent) and Last.fm (last year) taste signal for music events.
    Left out, not marked "unavailable", on failure: it's a filtering aid, not a
    digest section."""
    parts = []
    labels = spotify_accounts(settings)
    if labels:
        try:
            parts.append("Spotify, last ~6 months:\n" + top_artists(settings, labels[0]))
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("digest: spotify top artists unavailable: %s", exc)
    if settings.lastfm_api_key and settings.lastfm_user:
        try:
            parts.append("Last.fm, last 12 months:\n" + lastfm.summary(settings))
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("digest: last.fm top artists unavailable: %s", exc)
    return "\n\n".join(parts)


def build_payload(settings: Settings, conn: sqlite3.Connection, *, record: bool) -> dict:
    mute_list = mutes.load(settings.muted_file)
    return {
        "as_of": date.today().isoformat(),
        "home": settings.prefs["home"]["label"],
        "calendar": _collect_calendar(settings),
        "events": _collect_events(settings, conn, mute_list, record),
        "prices": _collect_prices(settings, conn, mute_list, record),
        "stale_pages": _collect_stale(settings, conn, mute_list, record),
        "interests": _interests(settings),
        "top_artists": _top_artists(settings),
    }


# --- The weekly digest --------------------------------------------------------------


def _archive_path(settings: Settings) -> Path:
    return settings.digests_dir / f"{date.today().isoformat()}.md"


def run_digest(settings: Settings, *, dry_run: bool = False) -> str:
    with db.session(settings.db_path) as conn:
        payload = build_payload(settings, conn, record=not dry_run)

    reply = asyncio.run(agent.ask(_prompt(payload), agent.digest_profile(settings), settings.claude_cli))
    if not reply.ok:
        raise RuntimeError(reply.error or "digest generation produced no reply")

    if not dry_run and reply.text.strip():
        path = _archive_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reply.text + "\n", encoding="utf-8")
        send_dm(settings, reply.text)

    return reply.text


# --- The daily presale ping ----------------------------------------------------------


# A backstop, not the filter: taste matching below is what keeps this short.
# Without either, one morning's check DM'd ~1000 events.
MAX_PRESALE_LINES = 10


_NOT_INTERESTED = re.compile(r"^#+\s*not interested\b.*?(?=^#+\s|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL)


def positive_interests(text: str) -> str:
    """interests.md minus its "Not interested" section. Naming a team there
    ("no Blues games") must not make that team's presale match."""
    return _NOT_INTERESTED.sub("", text)


def _taste(settings: Settings) -> tuple[set[str], str]:
    """(Spotify and Last.fm top artist names, the positive parts of
    facts/interests.md), all lowercased. Each source fails on its own."""
    names: set[str] = set()
    labels = spotify_accounts(settings)
    if labels:
        try:
            names |= top_artist_names(settings, labels[0])
        except Exception as exc:  # noqa: BLE001 - the other sources still work
            log.warning("presale: spotify taste unavailable: %s", exc)
    if settings.lastfm_api_key and settings.lastfm_user:
        try:
            names |= lastfm.artist_names(settings)
        except Exception as exc:  # noqa: BLE001 - the other sources still work
            log.warning("presale: last.fm taste unavailable: %s", exc)
    return names, positive_interests(_interests(settings)).lower()


def matches_taste(event: dict, artists: set[str], interests: str) -> bool:
    """True if any act on the bill is a Spotify/Last.fm top artist or is named in
    interests.md (as a whole word, so "Tool" doesn't match "toolbox")."""
    for act in event.get("attractions") or ([event["attraction"]] if event.get("attraction") else []):
        name = act.lower()
        if name in artists or re.search(rf"(?<!\w){re.escape(name)}(?!\w)", interests):
            return True
    return False


def run_presale_check(settings: Settings, *, dry_run: bool = False) -> str:
    """Presales opening today for artists the owner actually listens to or has
    named in facts/interests.md. Quiet by design: nothing to report means
    nothing is sent, and a failed check is logged rather than DM'd - an
    unattended daily job that pings an error every morning is exactly the
    noise both CLAUDE.md's anti-nag rule and the owner rule out. ``--dry-run``
    still surfaces a failure in its return value, since that path is a human
    watching a terminal, not an unattended morning ping.
    """
    mute_list = mutes.load(settings.muted_file)
    try:
        events = ticketmaster.presales_starting(settings, date.today())
    except Exception as exc:  # noqa: BLE001 - logged, never DM'd (see docstring)
        log.warning("presale check failed: %s", exc)
        return f"(check failed, nothing sent: {exc})" if dry_run else ""

    artists, interests = _taste(settings)
    wanted = [e for e in events if matches_taste(e, artists, interests)]
    log.info("presale check: %d on sale today, %d match taste", len(events), len(wanted))

    with db.session(settings.db_path) as conn:
        lines = []
        for event in wanted:
            iid = ticketmaster.item_id(event, "presale")
            if mutes.is_muted(iid, mute_list):
                continue
            if len(lines) >= MAX_PRESALE_LINES:
                lines.append(f"...and {len(wanted) - MAX_PRESALE_LINES} more.")
                break
            if not dry_run:
                db.record_surfaced(conn, iid, "presale", event["name"])
            lines.append(ticketmaster.format_event(event))

    if not lines:
        return ""

    text = "**Presale today**\n" + "\n".join(lines)
    if not dry_run:
        send_dm(settings, text)
    return text
