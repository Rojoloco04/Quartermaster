"""The digest's JSON as one Discord message. Code owns every header, bold and
link, so the layout is the same every day; the model only ever contributes the
short "why" on an event it picked.

Discord renders ``##``/``###`` headings, ``-#`` subtext and masked links; a
link written ``[label](<url>)`` gets no preview card, so twenty links don't
become twenty embeds.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .listings import clock, day_label

_MARKDOWN = re.compile(r"([*_~`|\\])")


def esc(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text or "")


def link(label: str, url: str) -> str:
    return f"[{esc(label)}](<{url}>)" if url else esc(label)


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _local(utc_iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(utc_iso.replace("Z", "+00:00")).astimezone()
    except (AttributeError, ValueError):
        return None


def _moment(utc_iso: str, today: date) -> str:
    when = _local(utc_iso)
    return f"{day_label(when.date(), today)}, {clock(f'{when:%H:%M}')}" if when else ""


def headline(item: dict) -> str:
    """**Act** & **Act** · Title — *why*"""
    acts = " & ".join(f"**{esc(a)}**" for a in item["acts"][:3])
    title = esc(item["title"])
    line = f"{acts} · {title}" if acts and title else acts or f"**{title}**"
    if item.get("why"):
        line += f" — *{esc(item['why'])}*"
    elif item.get("reminder"):
        line += " — *this week*"
    return line


def when(item: dict, today: date) -> str:
    first, last = _date(item["first_date"]), _date(item["last_date"])
    if first and last and last != first:
        return f"{day_label(first, today)} – {day_label(last, today)}"
    time = clock(item["time"]) if item.get("time") else ""
    return day_label(first, today) + (f", {time}" if time else "")


def where(item: dict) -> str:
    place = ", ".join(esc(p) for p in (item.get("venue"), item.get("city")) if p)
    if item.get("distance_miles") is not None and item["distance_miles"] >= 1:
        place += f" · {round(item['distance_miles'])} mi"
    return place


def links(item: dict) -> str:
    return " · ".join(link(l["label"], l["url"]) for l in item["listings"])


def event_lines(item: dict, today: date) -> list[str]:
    return [headline(item), f"-# {when(item, today)} · {where(item)} · {links(item)}"]


def onsale_lines(item: dict, today: date) -> list[str]:
    detail = [f"On sale {_moment(item['onsale_at'], today)}" if item.get("onsale_at") else "On sale soon"]
    presale = item.get("presale")
    if presale and (_local(presale["starts_at"]) or datetime.min.astimezone()).date() >= today:
        detail.append(f"{esc(presale['name'])} {_moment(presale['starts_at'], today)}")
    detail += [f"show {when(item, today)}", where(item), links(item)]
    return [headline(item), "-# " + " · ".join(d for d in detail if d)]


def _calendar(section: dict, today: date) -> list[str]:
    lines = []
    for day in section["days"]:
        parts = []
        for ev in day["events"]:
            title = esc(ev["title"])
            if ev["all_day"]:
                parts.append(title)
            else:
                span = clock(ev["start"]) + (f"–{clock(ev['end'])}" if ev.get("end") else "")
                parts.append(f"{title} ({span})")
        lines.append(f"**{day_label(_date(day['date']), today)}** · " + " · ".join(parts))
    return lines


def _events(section: dict, today: date) -> list[str]:
    lines, band = [], None
    for item in section["items"]:
        if item.get("band") != band:
            band = item.get("band")
            if band:
                lines.append(f"__{esc(band.replace('_', ' ').capitalize())}__")
        lines += event_lines(item, today)
    return lines


def _onsales(section: dict, today: date) -> list[str]:
    lines = [line for item in section["items"] for line in onsale_lines(item, today)]
    if section.get("more"):
        lines.append(f"-# …and {section['more']} more matching your taste.")
    return lines


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _prices(section: dict) -> list[str]:
    lines = []
    for d in section.get("drops") or []:
        pct = round(100 * (d["old_cents"] - d["new_cents"]) / d["old_cents"]) if d["old_cents"] else 0
        lines.append(f"**{esc(d['item_name'])}** {_money(d['old_cents'])} → **{_money(d['new_cents'])}**"
                     f" (−{pct}%) · {link('link', d['url'])}")
    if section.get("failures"):
        lines.append("-# Couldn't check: " + ", ".join(esc(f["item_name"]) for f in section["failures"]))
    return lines


def _notion(section: dict) -> list[str]:
    return [
        f"**{esc(p['title'] or 'Untitled')}** · untouched {p['days_untouched']} days, {esc(p['reason'])}"
        + (f" · {link('open', p['url'])}" if p.get("url") else "")
        for p in section["stale"]
    ]


SECTIONS = (
    ("calendar", "📅 Calendar", "days"),
    ("onsales", "🎟️ On sale soon", "items"),
    ("events", "🎤 Events", "items"),
    ("prices", "💸 Wishlist", None),
    ("notion", "🗂️ Notion", "stale"),
)


def render(digest: dict) -> str:
    """The message, or "" when there is nothing at all to say."""
    today = date.fromisoformat(digest["date"])
    body = []
    for key, heading, list_key in SECTIONS:
        section = digest.get(key)
        if not section:
            continue
        if "unavailable" in section:
            body += [f"### {heading}", f"-# Couldn't check: {esc(section['unavailable'])}", ""]
            continue
        if list_key and not section.get(list_key):
            continue
        lines = {
            "calendar": lambda: _calendar(section, today),
            "onsales": lambda: _onsales(section, today),
            "events": lambda: _events(section, today),
            "prices": lambda: _prices(section),
            "notion": lambda: _notion(section),
        }[key]()
        if lines:
            body += [f"### {heading}", *lines, ""]
    if not body:
        return ""
    return f"## {today:%A, %B} {today.day}\n" + "\n".join(body).rstrip()
