"""Ticketmaster listings into digest items.

One show is often sold as several listings: each day of a festival plus a
weekend bundle, or each night of a run. Listings at the same venue, by the same
headliner (or, with no act, the same name up to its first " - "), starting
within ``GROUP_DAYS`` of each other become one item with a link per listing.

An item is a plain dict - the digest's JSON - whose listing fields are the
``db.listings`` columns of the same names:

    {"acts", "title", "first_date", "last_date", "time", "venue", "city",
     "distance_miles", "band", "bar", "onsale_at", "presale",
     "listings": [{"id", "label", "url"}], "why", "reminder"}
"""

from __future__ import annotations

import re
from datetime import date

GROUP_DAYS = 3

_SEP = re.compile(r"\s+[-–|:]\s+|\s*:\s+|\s+\(")


def _stem(name: str) -> str:
    return _SEP.split(name.lower(), 1)[0].strip()


def group(listings: list[dict]) -> list[list[dict]]:
    """Listings into groups, each sorted by date, groups by their first date."""
    groups: list[list[dict]] = []
    open_by_key: dict[tuple[str, str], list[dict]] = {}
    for listing in sorted(listings, key=lambda l: (l.get("local_date") or "", l.get("local_time") or "")):
        acts = listing.get("acts") or []
        key = ((listing.get("venue") or "").lower(), acts[0].lower() if acts else _stem(listing["name"]))
        current = open_by_key.get(key)
        if current and _days_between(current[-1], listing) <= GROUP_DAYS:
            current.append(listing)
        else:
            current = [listing]
            open_by_key[key] = current
            groups.append(current)
    return groups


def _date(listing: dict) -> date | None:
    try:
        return date.fromisoformat(listing.get("local_date") or "")
    except ValueError:
        return None


def _days_between(a: dict, b: dict) -> int:
    da, db_ = _date(a), _date(b)
    return abs((db_ - da).days) if da and db_ else 0


def tidy_caps(text: str) -> str:
    """"JOHN SUMMIT - CTRL ESCAPE ARENA TOUR" reads as shouting; short
    all-caps words (LCS, DJ, USA) are kept as initialisms."""
    if not text.isupper():
        return text
    return " ".join(w if len(w) <= 3 and w.isalpha() else w.capitalize() for w in text.split(" "))


_PART = re.compile(r"\s*[:|]\s+|\s+[-–]\s+")


def _split_names(names: list[str]) -> tuple[str, list[str]]:
    """(title, one label per name) for one show's listings, from the parts of
    their names: the parts they all share make the title, and the first part
    that differs labels each listing ("" where it has none). "LCS FINALS",
    "LCS FINALS - TWO-DAY BUNDLE" -> ("LCS FINALS", ["", "TWO-DAY BUNDLE"]);
    "Friday Pass: X", "Saturday Pass: X" -> ("X", ["Friday Pass", "Saturday Pass"])."""
    parts = [_PART.split(n.strip()) for n in names]
    width = max(len(p) for p in parts)
    columns = [[p[i] if i < len(p) else None for p in parts] for i in range(width)]
    same = [c[0] is not None and len({(x or "").lower() for x in c}) == 1 for c in columns]
    title = " - ".join(c[0] for c, s in zip(columns, same) if s)
    labels = []
    for p in parts:
        labels.append(next((p[i] for i in range(len(p)) if not same[i]), ""))
    return title, labels


def _strip_act(title: str, acts: list[str]) -> str:
    """"John Summit - Ctrl Escape Tour" by John Summit is "Ctrl Escape Tour"."""
    if acts and title.lower().startswith(acts[0].lower()):
        return title[len(acts[0]):].lstrip(" -–|:,")
    return title


def day_label(d: date | None, today: date | None = None) -> str:
    if d is None:
        return "date TBA"
    if today is not None and d == today:
        return "Today"
    if today is not None and (d - today).days == 1:
        return "Tomorrow"
    return f"{d:%a %b} {d.day}"


def clock(hhmm: str) -> str:
    """"19:30" -> "7:30 pm", "20:00" -> "8 pm"."""
    try:
        hour, minute = (int(x) for x in hhmm.split(":")[:2])
    except ValueError:
        return ""
    suffix = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}" if minute else f"{hour12} {suffix}"


def item(members: list[dict]) -> dict:
    """One group of listings as a digest item (the JSON shape above)."""
    first, last = members[0], members[-1]
    acts = first.get("acts") or []
    names = [m["name"] for m in members]
    if len(members) == 1:
        title, parts = names[0], ["Tickets"]
    else:
        title, parts = _split_names(names)
        if not title and not acts:
            title = names[0]
    title = tidy_caps(_strip_act(title, acts))

    links = []
    for m, part in zip(members, parts):
        label = tidy_caps(part) if part else day_label(_date(m))
        links.append({"id": m["id"], "label": label, "url": m.get("url") or ""})
    # Same label twice (a matinee and an evening show): tell them apart by
    # time where that differs, else number them.
    labels = [l["label"] for l in links]
    for i, (link, m) in enumerate(zip(links, members)):
        if labels.count(labels[i]) > 1:
            times = {clock(x.get("local_time") or "") for x, lab in zip(members, labels) if lab == labels[i]}
            link["label"] += f", {clock(m['local_time'])}" if len(times) > 1 and m.get("local_time") else f" ({i + 1})"

    presales = sorted((p for m in members for p in m.get("presales") or []), key=lambda p: p["starts_at"])
    return {
        "acts": acts,
        "title": "" if acts and title.lower() == acts[0].lower() else title,
        "first_date": first.get("local_date") or "",
        "last_date": last.get("local_date") or "",
        "time": first.get("local_time") or "",
        "venue": first.get("venue") or "",
        "city": first.get("city") or "",
        "distance_miles": first.get("distance_miles"),
        "band": first.get("band"),
        "bar": first.get("bar"),
        "onsale_at": min((m["onsale_at"] for m in members if m.get("onsale_at")), default=""),
        "presale": presales[0] if presales else None,
        "listings": links,
        "why": "",
        "reminder": False,
    }
