"""The daily digest: calendar, on-sales, events, wishlist drops, stale pages.

Collectors are plain Python: each fetches, filters through the mute list and
hands back plain data, and each failing degrades only its own section. The
result is the digest's JSON (see ``listings`` for an item's shape), which
``render`` turns into one Discord message - so the layout is fixed and code
owns every name, date and link.

The model does one thing: from the events that are new since the last digest,
it picks the ones worth a line and says why in a few words (``output_schema``,
answering by id). It never sees on-sales, prices or the calendar, and never
restates a fact.

How often things come back, with a daily digest and a month's window:
- an event: when first found (if picked), then once more in the week it
  happens; an event the model passed over isn't offered again.
- an on-sale: once.
- a stale Notion page: at most weekly; unreadable wishlist prices: weekly, on
  ``digest.weekday``.
Muting still silences any of them for good.

``--dry-run`` collects real data and asks the model (so the preview is honest)
but marks nothing shown, sends nothing and archives nothing. Listings and price
checks are recorded either way: they are observations, not showings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .. import agent, db
from ..config import Settings
from ..integrations import google, prices, ticketmaster
from ..knowledge import mutes, stale
from . import taste
from .listings import group, item
from .render import render
from .taste import matches as matches_taste, positive_interests


log = logging.getLogger(__name__)

__all__ = ["run_digest", "build", "render", "matches_taste", "positive_interests"]

MAX_EVENT_PICKS = 12
# New shows offered to the model per distance band per day; the rest wait.
MAX_OFFERED_PER_BAND = 60
MAX_ONSALE_ITEMS = 10
REMINDER_DAYS = 7
STALE_EVERY_DAYS = 7

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
                "required": ["id", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["picks"],
    "additionalProperties": False,
}

_PICK_RULES = f"""\
Pick the events worth telling the owner about in this morning's digest. Each \
candidate below is one show near them (one may be sold as several listings; \
it is still one show). None has been offered before.

- Each has a distance band with a bar: low = a casual local mention is fine; \
medium = should genuinely match their interests; high = must clearly be worth \
the trip.
- Judge by their interests and top artists below. Don't pick filler.
- At most {MAX_EVENT_PICKS}. Picking none is fine.
- For each pick, `why`: at most 10 words on why this one for them (e.g. \
"your #2 artist this year", "you said you want to see more jazz"). No emoji. \
Don't repeat the name, date or venue: those are shown already.
- Answer by the candidate's id only.
"""


def _unavailable(section: str, exc: Exception) -> dict:
    log.warning("digest: %s unavailable: %s", section, exc)
    return {"unavailable": str(exc) or type(exc).__name__}


def _iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


# --- Calendar --------------------------------------------------------------------


def ignored(event: dict, terms: list[str]) -> bool:
    """An all-day entry whose title contains one of ``digest.ignore_calendar``
    (reminders, anniversaries) isn't a plan worth a line."""
    title = (event.get("summary") or "").lower()
    return "date" in event.get("start", {}) and any(t.lower() in title for t in terms if t)


def calendar_days(events: list[dict], terms: list[str]) -> list[dict]:
    """Google events as ``[{date, events: [{title, all_day, start, end}]}]``. Pure."""
    days: dict[str, list[dict]] = {}
    for ev in events:
        if ignored(ev, terms):
            continue
        start, end = ev.get("start", {}), ev.get("end", {})
        if "date" in start:
            day, entry = start["date"], {"all_day": True, "start": "", "end": ""}
        else:
            s, e = start.get("dateTime", ""), end.get("dateTime", "")
            day = s[:10]
            entry = {"all_day": False, "start": s[11:16], "end": e[11:16] if e[:10] == day else ""}
        days.setdefault(day, []).append({"title": ev.get("summary") or "(no title)", **entry})
    return [{"date": d, "events": evs} for d, evs in sorted(days.items())]


def _collect_calendar(settings: Settings, today: date) -> dict:
    prefs = settings.prefs["digest"]
    try:
        events = google.calendar_events(
            settings, today.isoformat(), (today + timedelta(days=int(prefs["calendar_days_ahead"]))).isoformat())
    except Exception as exc:  # noqa: BLE001 - any API's failure costs only its section
        return _unavailable("calendar", exc)
    return {"days": calendar_days(events, list(prefs.get("ignore_calendar") or []))}


# --- Ticketmaster: events and on-sales ---------------------------------------------


def _member_ids(members: list[dict]) -> list[str]:
    return [m["id"] for m in members]


def classify_events(groups: list[list[dict]], rows: dict, today: date) -> tuple[list, list]:
    """(new, reminders). New: no listing in it was ever offered to the model.
    Reminder: shown before, happens within ``REMINDER_DAYS``, and last shown
    before that week began. Pure given ``rows`` (listing id -> db row)."""
    new, reminders = [], []
    for members in groups:
        seen = [rows[i] for i in _member_ids(members) if i in rows]
        if not any(r["considered_at"] for r in seen):
            new.append(members)
            continue
        shown = max((r["event_shown_at"] or "" for r in seen), default="")
        first = _iso_date(members[0].get("local_date") or "")
        if shown and first and 0 <= (first - today).days <= REMINDER_DAYS:
            if shown[:10] < (first - timedelta(days=REMINDER_DAYS)).isoformat():
                reminders.append(members)
    return new, reminders


def offer(new: list[list[dict]], likes) -> list[list[dict]]:
    """Which new groups the model sees today: per band, at most
    ``MAX_OFFERED_PER_BAND``, the ones ``likes`` first, then spread across the
    month (the first show of each date, then the second of each...). The rest
    stay unconsidered and come up on a later morning. Soonest-first filled a
    500-mile band with tonight's shows every day and never reached next month."""
    by_band: dict[str, list[list[dict]]] = {}
    for members in new:
        by_band.setdefault(members[0].get("band") or "", []).append(members)
    offered = []
    for groups in by_band.values():
        nth_of_day: dict[str, int] = {}
        keyed = []
        for g in sorted(groups, key=lambda g: g[0].get("local_date") or ""):
            day = g[0].get("local_date") or ""
            nth_of_day[day] = nth_of_day.get(day, -1) + 1
            keyed.append(((not any(likes(m) for m in g), nth_of_day[day], day), g))
        offered += [g for _, g in sorted(keyed, key=lambda pair: pair[0])[:MAX_OFFERED_PER_BAND]]
    return offered


def _collect_events(settings, conn, today, mute_list, artists, interests_text):
    try:
        banded = ticketmaster.events_for_bands(settings, int(settings.prefs["events"]["window_days"]))
    except Exception as exc:  # noqa: BLE001
        return _unavailable("events", exc), [], []
    found = [e for events in banded.values() for e in events]
    for listing in found:
        db.upsert_listing(conn, listing)
    kept = [e for e in found if not mutes.is_muted(ticketmaster.item_id(e), mute_list)]
    new, reminders = classify_events(group(kept), db.listings_by_id(conn, [e["id"] for e in kept]), today)
    offered = offer(new, lambda e: taste.matches(e, artists, interests_text))
    log.info("events: %d listings, %d new shows, %d offered, %d reminders", len(found), len(new), len(offered), len(reminders))
    return None, offered, reminders


def _collect_onsales(settings, conn, today, mute_list, artists, interests_text):
    try:
        found = ticketmaster.onsales_between(settings, today, int(settings.prefs["events"]["onsale_days"]))
    except Exception as exc:  # noqa: BLE001
        return _unavailable("on-sales", exc), []
    for listing in found:
        db.upsert_listing(conn, listing)
    wanted = [e for e in found if taste.matches(e, artists, interests_text)
              and not mutes.is_muted(ticketmaster.item_id(e, "presale"), mute_list)]
    rows = db.listings_by_id(conn, [e["id"] for e in wanted])
    fresh = [g for g in group(wanted) if not any(rows.get(i) and rows[i]["onsale_shown_at"] for i in _member_ids(g))]
    log.info("on-sales: %d in the next window, %d match taste, %d not shown before", len(found), len(wanted), len(fresh))
    return None, sorted(fresh, key=lambda g: min(m.get("onsale_at") or "~" for m in g))


async def pick_events(settings: Settings, candidates: list[dict]) -> dict[str, str]:
    """{candidate id: why} for the events the model picks."""
    payload = [
        {"id": c["id"], "acts": c["acts"], "title": c["title"], "dates": c["first_date"] +
         (f" to {c['last_date']}" if c["last_date"] != c["first_date"] else ""), "venue": c["venue"],
         "city": c["city"], "distance_miles": c["distance_miles"], "band": c["band"], "bar": c["bar"],
         "listings": len(c["listings"])}
        for c in candidates
    ]
    prompt = (
        _PICK_RULES
        + "\n\nTheir interests (facts/interests.md):\n" + taste.interests(settings)
        + "\n\nTheir top artists:\n" + (taste.top_artists_text(settings) or "(unavailable)")
        + "\n\nCandidates:\n" + json.dumps(payload, indent=1, default=str)
    )
    reply = await agent.ask(prompt, agent.digest_profile(settings, PICK_SCHEMA), settings.claude_cli)
    if not reply.ok or reply.structured is None:
        raise RuntimeError(reply.error or "the model returned no picks")
    known = {c["id"] for c in candidates}
    picks = {p["id"]: p["why"].strip() for p in reply.structured.get("picks") or [] if p.get("id") in known}
    return dict(list(picks.items())[:MAX_EVENT_PICKS])


# --- Wishlist and Notion ---------------------------------------------------------------


def _collect_prices(settings, conn, today, mute_list) -> dict:
    try:
        result = prices.check_all(settings, conn)
    except Exception as exc:  # noqa: BLE001
        return _unavailable("prices", exc)
    drops = [d for d in result["drops"] if not mutes.is_muted(f"price:{d['block_id']}", mute_list)]
    weekly = today.strftime("%A").lower() == str(settings.prefs["digest"]["weekday"]).lower()
    return {"drops": drops, "failures": result["failures"] if weekly else []}


_URL = re.compile(r'^url:\s*"?([^"\n]+)"?\s*$', re.MULTILINE)


def _page_url(vault_path: str) -> str:
    try:
        with open(vault_path, encoding="utf-8") as f:
            head = f.read(1000)
    except OSError:
        return ""
    m = _URL.search(head)
    return m.group(1).strip() if m else ""


def _collect_stale(settings, conn, mute_list, now: datetime) -> dict:
    cutoff = (now - timedelta(days=STALE_EVERY_DAYS)).isoformat(timespec="seconds")
    pages = []
    for page in stale.find_stale_pages(settings, conn):
        iid = stale.item_id(page["page_id"])
        if mutes.is_muted(iid, mute_list) or (db.last_surfaced(conn, iid) or "") > cutoff:
            continue
        pages.append({**page, "url": _page_url(page["vault_path"])})
    return {"stale": pages}


# --- Putting it together ---------------------------------------------------------------


def _band_order(settings: Settings) -> dict[str, int]:
    bands = sorted(settings.prefs["events"]["bands"], key=lambda b: b["max_miles"])
    return {b["name"]: i for i, b in enumerate(bands)}


def build(settings: Settings, conn: sqlite3.Connection, today: date | None = None) -> tuple[dict, dict]:
    """(the digest's JSON, what to mark if it's sent). Asks the model only if
    there are new events to judge."""
    today = today or date.today()
    now = datetime.now(timezone.utc)
    mute_list = mutes.load(settings.muted_file)
    artists = taste.artist_names(settings)
    interests_text = positive_interests(taste.interests(settings)).lower()
    marks: dict[str, list[str]] = {"considered": [], "event": [], "onsale": [], "surfaced": []}

    digest: dict = {"date": today.isoformat(), "calendar": _collect_calendar(settings, today)}

    failed, onsale_groups = _collect_onsales(settings, conn, today, mute_list, artists, interests_text)
    if failed:
        digest["onsales"] = failed
    else:
        shown = onsale_groups[:MAX_ONSALE_ITEMS]
        digest["onsales"] = {"items": [item(g) for g in shown], "more": max(0, len(onsale_groups) - len(shown))}
        marks["onsale"] = [i for g in shown for i in _member_ids(g)]

    failed, new, reminders = _collect_events(settings, conn, today, mute_list, artists, interests_text)
    if failed:
        digest["events"] = failed
    else:
        candidates = [{**item(members), "id": f"e{n}"} for n, members in enumerate(new, 1)]
        picks, pick_error = {}, ""
        try:
            picks = asyncio.run(pick_events(settings, candidates)) if candidates else {}
            marks["considered"] = [i for g in new for i in _member_ids(g)]
        except Exception as exc:  # noqa: BLE001 - not marked considered, so offered again tomorrow
            pick_error = str(exc) or type(exc).__name__
            log.warning("digest: picking events failed: %s", exc)
        chosen = [({**c, "why": picks[c["id"]]}, m) for c, m in zip(candidates, new) if c["id"] in picks]
        chosen += [({**item(m), "reminder": True}, m) for m in reminders]
        order = _band_order(settings)
        chosen.sort(key=lambda pair: (order.get(pair[0]["band"], 99), pair[0]["first_date"]))
        for entry, _ in chosen:
            entry.pop("id", None)  # a per-run handle for the model, not part of the item
        if pick_error and not chosen:
            digest["events"] = {"unavailable": f"couldn't pick events ({pick_error})"}
        else:
            digest["events"] = {"items": [entry for entry, _ in chosen]}
        marks["event"] = [i for _, m in chosen for i in _member_ids(m)]

    digest["prices"] = _collect_prices(settings, conn, today, mute_list)
    digest["notion"] = _collect_stale(settings, conn, mute_list, now)
    marks["surfaced"] = (
        [("price", f"price:{d['block_id']}", d["item_name"]) for d in digest["prices"].get("drops") or []]
        + [("stale", stale.item_id(p["page_id"]), p["title"] or "") for p in digest["notion"]["stale"]]
    )
    return digest, marks


def record(conn: sqlite3.Connection, marks: dict) -> None:
    for what in ("considered", "event", "onsale"):
        db.mark_listings(conn, marks[what], what)
    for kind, iid, summary in marks["surfaced"]:
        db.record_surfaced(conn, iid, kind, summary)
    removed = db.prune(conn)
    if removed:
        log.info("state.db pruned: %s", removed)


def run_digest(settings: Settings, *, dry_run: bool = False) -> str:
    from ..discord_bot.send import send_dm

    with db.session(settings.db_path) as conn:
        digest, marks = build(settings, conn)
        text = render(digest)
        if dry_run or not text:
            return text
        settings.digests_dir.mkdir(parents=True, exist_ok=True)
        stem = settings.digests_dir / digest["date"]
        stem.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
        stem.with_suffix(".json").write_text(json.dumps(digest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        send_dm(settings, text)
        record(conn, marks)
    return text
