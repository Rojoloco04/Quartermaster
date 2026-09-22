"""Stale Notion pages.

Untouched for a long time is not enough on its own - a reference page written
once and never needing edits isn't stale, it's finished. A page only counts
here if it is old *and* still looks unfinished: an open checkbox, an empty
section, a stub too short to be anything else.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings

MIN_BODY_CHARS = 40

_CHECKBOX_OPEN = re.compile(r"^[ \t]*-\s+\[ \]", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_TODO = re.compile(r"^\s*(TODO|TBD)\b", re.MULTILINE | re.IGNORECASE)


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def looks_unfinished(markdown_body: str) -> tuple[bool, str]:
    """Whether a page's content still looks like a work in progress, and why.

    Pure and unit-testable on plain strings - no file I/O here.
    """
    body = _strip_frontmatter(markdown_body).strip()

    if len(body) < MIN_BODY_CHARS:
        return True, "page is essentially empty"

    open_boxes = len(list(_CHECKBOX_OPEN.finditer(body)))
    if open_boxes:
        return True, f"{open_boxes} open checkbox{'es' if open_boxes != 1 else ''}"

    if _TODO.search(body):
        return True, "contains a TODO/TBD marker"

    headings = list(_HEADING.finditer(body))
    for i, heading in enumerate(headings):
        section_end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        section_body = body[heading.end() : section_end].strip()
        if not section_body:
            return True, f"empty section '{heading.group(2).strip()}'"

    return False, ""


def item_id(page_id: str) -> str:
    return f"stale:{page_id}"


def find_stale_pages(settings: Settings, conn: sqlite3.Connection) -> list[dict]:
    """Mirrored pages old enough *and* still looking unfinished.

    Returns ``{page_id, title, vault_path, reason, days_untouched}`` for each.
    """
    threshold_days = settings.prefs["notion"]["stale_after_days"]
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=threshold_days)

    rows = conn.execute(
        "SELECT page_id, vault_path, title, last_edited_at FROM notion_pages WHERE archived = 0"
    ).fetchall()

    stale: list[dict] = []
    for row in rows:
        try:
            edited = datetime.fromisoformat((row["last_edited_at"] or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if edited.tzinfo is None:
            edited = edited.replace(tzinfo=timezone.utc)
        if edited > cutoff:
            continue

        path = Path(row["vault_path"])
        if not path.exists():
            continue

        unfinished, reason = looks_unfinished(path.read_text(encoding="utf-8"))
        if not unfinished:
            continue

        stale.append(
            {
                "page_id": row["page_id"],
                "title": row["title"],
                "vault_path": row["vault_path"],
                "reason": reason,
                "days_untouched": (now - edited).days,
            }
        )

    return stale
