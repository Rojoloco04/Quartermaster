"""DMs held for the morning.

The model-using chores run between 03:00 and 04:00, so they never share the
owner's 5-hour usage window, but nothing they say should buzz a phone at 3am.
Before ``digest.hour`` a job holds its DM here instead of sending it, and the
bot (always running) delivers what's held once that hour comes: the digest
first, then the rest in the order held. A message stays here until it is
sent, so a bot that's down at 08:00 delivers late rather than never.

Plain file ops, no discord import: the jobs that hold and the bot that
delivers are different processes, and ``send`` imports from ``bot``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from ..config import current_prefs, Settings


def held_path(settings: Settings) -> Path:
    return settings.system_dir / "held-dms.json"


def morning_hour(settings: Settings) -> int:
    return int(current_prefs(settings)["digest"]["hour"])


def quiet(settings: Settings, now: datetime | None = None) -> bool:
    """Night: from midnight until ``digest.hour``."""
    return (now or datetime.now()).hour < morning_hour(settings)


def load(settings: Settings) -> list[dict]:
    try:
        items = json.loads(held_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return items if isinstance(items, list) else []


def _write(settings: Settings, items: list[dict]) -> None:
    path = held_path(settings)
    if not items:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def hold(settings: Settings, text: str, what: str) -> None:
    """Keep ``text`` for the morning. ``what`` ("digest", "reconcile") orders
    delivery: the digest goes first."""
    items = load(settings) + [{
        "id": uuid.uuid4().hex, "what": what, "text": text,
        "held_at": datetime.now().isoformat(timespec="seconds"),
    }]
    _write(settings, items)


def due(settings: Settings, now: datetime | None = None) -> list[dict]:
    """What to deliver now: everything held, once it's morning."""
    if quiet(settings, now):
        return []
    return sorted(load(settings), key=lambda i: i.get("what") != "digest")


def delivered(settings: Settings, item_id: str) -> None:
    """Drop one sent message. Re-reads the file, so a message held meanwhile
    by another process isn't lost."""
    _write(settings, [i for i in load(settings) if i.get("id") != item_id])
