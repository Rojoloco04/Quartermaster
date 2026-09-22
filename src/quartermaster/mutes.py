"""The mute list.

The behavioural rule this project is built around:

    Reminding is the default. Muting is permanent, and one sentence from you
    is enough to do it.

Nothing is ever silenced automatically. An item keeps surfacing until you say
stop, and once you say stop it never comes back unless you delete the line.

This lives in markdown rather than the database on purpose. It is a record of
your preferences, it is small, and you should be able to open it, read why you
muted something, and un-mute it by deleting a line. A database row you can't
see is the wrong shape for that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

HEADER = """# Muted

Things I've been told to stop raising. **Delete a line to un-mute it.**

Reminding is the default — only what's listed here is silenced. Each entry is
`kind:key`. A mute also covers anything nested beneath it, so `event:artist/Tool`
silences every Tool event, not just one show. Leave the kind off (`artist/Tool`)
to silence everything about Tool: digest lines and presale pings alike.

"""

# - `stale:abc123` — Some page title — muted 2026-09-21 — "reason"
_LINE = re.compile(r"^\s*-\s+`([^`]+)`(.*)$")


@dataclass(frozen=True)
class Mute:
    item_id: str
    note: str


def _ensure(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(HEADER, encoding="utf-8")


def load(path: Path) -> list[Mute]:
    if not path.exists():
        return []
    mutes: list[Mute] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if m:
            mutes.append(Mute(item_id=m.group(1).strip(), note=m.group(2).strip(" —-")))
    return mutes


def is_muted(item_id: str, mutes: list[Mute]) -> bool:
    """True if this item, or any parent scope of it, has been muted.

    Scope nesting uses '/' so a broad mute can cover a family of items:
    muting ``event:artist/Tool`` also silences ``event:artist/Tool/2026-11-02``.
    A mute with no ``kind:`` prefix covers every kind: ``artist/Tool`` silences
    both the digest's events line and the presale ping. Case-insensitive, since
    a mute is typed by a person and Ticketmaster's casing isn't predictable.
    """
    item = item_id.lower()
    bare = item.split(":", 1)[1] if ":" in item else item
    for mute in mutes:
        scope = mute.item_id.lower()
        candidate = item if ":" in scope else bare
        if candidate == scope or candidate.startswith(scope + "/"):
            return True
    return False


def add(path: Path, item_id: str, summary: str = "", reason: str = "") -> bool:
    """Record a permanent mute. Returns False if it was already muted.

    Appends rather than rewriting, so anything you've written in the file by
    hand survives.
    """
    _ensure(path)
    if is_muted(item_id, load(path)):
        return False

    parts = [f"- `{item_id}`"]
    if summary:
        parts.append(summary)
    parts.append(f"muted {date.today().isoformat()}")
    if reason:
        parts.append(f'"{reason}"')
    line = " — ".join(parts)

    existing = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(existing + "\n" + line + "\n", encoding="utf-8")
    return True

