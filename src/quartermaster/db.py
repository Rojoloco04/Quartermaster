"""Machine state.

This database holds only things that are derived or countable: price history,
what we've already shown you, Notion sync bookkeeping, event dedupe.

The rule, and it is load-bearing: **nothing here is the only copy of anything
you would miss.** Delete state.db and you lose price history and "already
showed you this" — annoying, not fatal. Everything irreplaceable is markdown in
the vault's git repo. If you ever find yourself wanting to store something here
that isn't recoverable, it belongs in a file instead.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

SCHEMA = """
-- One row per mirrored Notion page. Lets the daily pull skip unchanged pages
-- and lets the stale scan work without re-reading every file.
CREATE TABLE IF NOT EXISTS notion_pages (
    page_id        TEXT PRIMARY KEY,
    vault_path     TEXT NOT NULL,
    title          TEXT,
    last_edited_at TEXT,
    content_hash   TEXT,
    synced_at      TEXT NOT NULL,
    archived       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notion_pages_edited ON notion_pages(last_edited_at);

-- Append-only price observations. A drop is computed by comparing the latest
-- two successful checks for a url, which is exactly the query a file can't do.
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    item_name   TEXT,
    price_cents INTEGER,          -- NULL when ok = 0
    currency    TEXT DEFAULT 'USD',
    ok          INTEGER NOT NULL, -- 0 means "couldn't check", never "no change"
    note        TEXT,
    checked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_url_time ON price_history(url, checked_at DESC);

-- Everything nudge-able passes through here so we know whether it's new and
-- how often we've raised it. Muting is NOT stored here: mutes are a preference
-- you should be able to read and edit, so they live in 90-System/muted.md.
CREATE TABLE IF NOT EXISTS surfaced (
    item_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,    -- 'event' | 'price' | 'stale' | 'presale'
    summary     TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    times_shown INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_surfaced_kind ON surfaced(kind);

-- Events arrive from three overlapping radius bands and across weeks; this
-- keeps the same show from appearing twice.
CREATE TABLE IF NOT EXISTS events_seen (
    event_id   TEXT PRIMARY KEY,
    name       TEXT,
    starts_at  TEXT,
    band       TEXT,
    venue      TEXT,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events_seen(starts_at);

-- Notion edits outside the Claude page, waiting for the owner to press
-- Confirm in Discord. Nothing here is irreplaceable: an unapproved proposal
-- that is lost was never applied, and an applied one is in Notion (with the
-- replaced content backed up into the vault).
CREATE TABLE IF NOT EXISTS pending_writes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id    TEXT NOT NULL,
    page_title TEXT,
    mode       TEXT NOT NULL,    -- 'append' | 'replace' | 'delete'
    content    TEXT NOT NULL,
    why        TEXT,
    source     TEXT,             -- 'agent' | 'tidy'
    status     TEXT NOT NULL,    -- 'pending' | 'applied' | 'declined' | 'failed'
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_writes(status, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database, creating it and its schema if absent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL keeps the weekly digest job from blocking the always-on bot.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_surfaced(conn: sqlite3.Connection, item_id: str, kind: str, summary: str) -> int:
    """Note that an item was shown. Returns how many times it has now been shown.

    Callers use the count to vary tone — first mention reads differently from
    the fourth. Suppression is a separate concern handled by the mute list.
    """
    now = utcnow()
    conn.execute(
        """
        INSERT INTO surfaced (item_id, kind, summary, first_seen, last_seen, times_shown)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(item_id) DO UPDATE SET
            last_seen   = excluded.last_seen,
            times_shown = surfaced.times_shown + 1,
            summary     = excluded.summary
        """,
        (item_id, kind, summary, now, now),
    )
    row = conn.execute("SELECT times_shown FROM surfaced WHERE item_id = ?", (item_id,)).fetchone()
    return int(row["times_shown"])


def record_price_check(
    conn: sqlite3.Connection,
    url: str,
    item_name: str,
    price_cents: int | None,
    currency: str,
    ok: bool,
    note: str = "",
) -> None:
    """Append one price observation. Always append, never overwrite - the
    history is the point, and `latest_price` already knows to ignore failures."""
    conn.execute(
        """
        INSERT INTO price_history (url, item_name, price_cents, currency, ok, note, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (url, item_name, price_cents, currency, 1 if ok else 0, note, utcnow()),
    )


def record_event_seen(
    conn: sqlite3.Connection, event_id: str, name: str, starts_at: str, band: str, venue: str
) -> None:
    """Log an event the digest has surfaced. ON CONFLICT does nothing rather
    than updating, so `first_seen` stays the first time this show was found -
    the only fact this table exists to keep."""
    conn.execute(
        """
        INSERT INTO events_seen (event_id, name, starts_at, band, venue, first_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO NOTHING
        """,
        (event_id, name, starts_at, band, venue, utcnow()),
    )


def shown_count(conn: sqlite3.Connection, item_id: str) -> int:
    """How many times an item has been shown, without recording a new one.

    Used by a dry run: it needs the same "mentioned before" context a real
    run would use for tone, but must not itself count as a showing.
    """
    row = conn.execute("SELECT times_shown FROM surfaced WHERE item_id = ?", (item_id,)).fetchone()
    return int(row["times_shown"]) if row else 0


def latest_price(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    """Most recent *successful* check for a url.

    Failed checks are deliberately excluded: comparing against a failure would
    invent a price change out of a network error.
    """
    return conn.execute(
        """
        SELECT * FROM price_history
        WHERE url = ? AND ok = 1
        ORDER BY checked_at DESC, id DESC
        LIMIT 1
        """,
        (url,),
    ).fetchone()
