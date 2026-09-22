"""Notion edits that need the owner's say-so.

The Claude page and its sub-pages are the agent's own and are written directly
(``integrations.claude_page``). Everywhere else in Notion is the owner's, so a
change there is *proposed*: the agent calls the ``qm`` server's
``propose_notion_edit``, which only writes a row here. The bot then DMs the
owner a preview with Confirm/Cancel, and **code** applies it on Confirm.

The gate is a button press, not the model's judgement. An email or web page the
agent read can produce a proposal; it cannot approve one.

A ``replace`` or ``delete`` keeps the page's current content first, as markdown
in the vault's git repo, because Notion's own history is not something this code
can rely on. A delete moves the page to Notion's trash (restorable there), never
past it.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date
from pathlib import Path

from . import db
from .config import Settings
from .integrations.notion import NotionClient, NotionError

log = logging.getLogger(__name__)

MODES = ("append", "replace", "delete")
BACKED_UP = ("replace", "delete")
PREVIEW_CHARS = 700


def _norm(page_id: str) -> str:
    return page_id.replace("-", "").strip().lower()


def propose(
    conn: sqlite3.Connection, page_id: str, title: str, mode: str, content: str,
    why: str = "", source: str = "agent",
) -> int:
    if mode not in MODES:
        raise NotionError(f"mode must be one of {', '.join(MODES)}.")
    if mode != "delete" and not content.strip():
        raise NotionError("Nothing to write.")
    cur = conn.execute(
        """
        INSERT INTO pending_writes
            (page_id, page_title, mode, content, why, source, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (_norm(page_id), title, mode, content, why, source, db.utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pending_writes WHERE status = 'pending' ORDER BY id"
    ).fetchall()


def decide(conn: sqlite3.Connection, write_id: int, status: str) -> None:
    conn.execute(
        "UPDATE pending_writes SET status = ?, decided_at = ? WHERE id = ?",
        (status, db.utcnow(), write_id),
    )
    conn.commit()


def preview(row: sqlite3.Row) -> str:
    """What the owner sees before pressing anything. Built from the row's own
    fields, never from the model's prose."""
    title = row["page_title"] or row["page_id"]
    if row["mode"] == "delete":
        lines = [f"**Notion change #{row['id']}** — 🗑️ **Delete** *{title}*"]
        if row["why"]:
            lines.append(f"_{row['why']}_")
        lines.append("_Moves it and every page under it to Notion's trash, where you can restore it. "
                     "The page is saved to the vault first._")
        return "\n".join(lines)
    verb = "Append to" if row["mode"] == "append" else "**Replace** the contents of"
    body = (row["content"] or "").strip()
    if len(body) > PREVIEW_CHARS:
        body = body[:PREVIEW_CHARS] + f"\n… ({len(row['content']) - PREVIEW_CHARS} more characters)"
    lines = [f"**Notion change #{row['id']}** — {verb} *{title}*"]
    if row["why"]:
        lines.append(f"_{row['why']}_")
    lines.append(f"```\n{body}\n```")
    if row["mode"] == "replace":
        lines.append("_The current page is saved to the vault first._")
    return "\n".join(lines)


def backup_path(settings: Settings, row: sqlite3.Row) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", (row["page_title"] or row["page_id"]).lower()).strip("-")[:40]
    return settings.vault / "notion-backups" / f"{date.today().isoformat()}-{slug or 'page'}-{row['id']}.md"


def apply(settings: Settings, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Perform an approved write. Returns what to tell the owner."""
    settings.require("notion_token")
    try:
        with NotionClient(settings.notion_token or "") as client:
            if row["mode"] in BACKED_UP:
                # Keep what is about to be overwritten or trashed, in the vault's git repo.
                current = client.page_markdown(row["page_id"]).markdown
                path = backup_path(settings, row)
                path.parent.mkdir(parents=True, exist_ok=True)
                done = "replaced" if row["mode"] == "replace" else "deleted"
                path.write_text(
                    f"# Backup of {row['page_title'] or row['page_id']}\n"
                    f"Taken {db.utcnow()} before Quartermaster {done} it (change #{row['id']}).\n\n"
                    + current,
                    encoding="utf-8",
                )
            if row["mode"] == "delete":
                client.trash_page(row["page_id"])
                result = (f"🗑️ Moved *{row['page_title']}* to Notion's trash. Its contents: `{path.name}` "
                          "in the vault. The next sync drops it from the mirror.")
            elif row["mode"] == "replace":
                client.replace_markdown(row["page_id"], row["content"])
                result = f"✅ Replaced *{row['page_title']}*. Previous contents: `{path.name}` in the vault."
            else:
                client.append_markdown(row["page_id"], row["content"])
                result = f"✅ Appended to *{row['page_title']}*."
    except Exception as exc:  # noqa: BLE001 - report to the owner, never crash the bot
        log.exception("applying pending write %s failed", row["id"])
        decide(conn, row["id"], "failed")
        return f"❌ Couldn't apply change #{row['id']}: {exc}"
    decide(conn, row["id"], "applied")
    log.info("applied pending write %s (%s %s)", row["id"], row["mode"], row["page_id"])
    return result
