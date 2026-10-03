"""Machine state.

This database holds only things that are derived or countable: price history,
what we've already shown you, Notion sync bookkeeping, Ticketmaster listings.
``prune`` (run after each digest) keeps it from growing without bound.

The rule, and it is load-bearing: **nothing here is the only copy of anything
you would miss.** Delete state.db and you lose price history and "already
showed you this" — annoying, not fatal. Everything irreplaceable is markdown in
the vault's git repo. If you ever find yourself wanting to store something here
that isn't recoverable, it belongs in a file instead.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 3

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

-- Wishlist drops and stale pages: whether each is new and how often it's been
-- raised. Muting is NOT stored here: mutes are a preference you should be able
-- to read and edit, so they live in System/muted.md.
CREATE TABLE IF NOT EXISTS surfaced (
    item_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,    -- 'price' | 'stale'
    summary     TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    times_shown INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_surfaced_kind ON surfaced(kind);

-- One row per Ticketmaster listing, with the same fields the digest's JSON
-- uses for it (digest.Listing), plus when it was shown as an event and as an
-- on-sale. A digest item is a group of these (one show sold as day passes and
-- a bundle), so "shown" is checked across the group.
CREATE TABLE IF NOT EXISTS listings (
    id                TEXT PRIMARY KEY,   -- Ticketmaster's event id
    name              TEXT NOT NULL,
    acts              TEXT NOT NULL,      -- JSON list, headliner first
    local_date        TEXT,               -- YYYY-MM-DD at the venue
    local_time        TEXT,               -- HH:MM, '' when not announced
    venue             TEXT,
    city              TEXT,
    distance_miles    REAL,
    url               TEXT,
    onsale_at         TEXT,               -- public on-sale, UTC ISO
    presales          TEXT NOT NULL,      -- JSON list of {name, starts_at}
    first_seen        TEXT NOT NULL,
    considered_at     TEXT,               -- first offered to the model
    event_shown_at    TEXT,               -- last shown in the events section
    event_times_shown INTEGER NOT NULL DEFAULT 0,
    onsale_shown_at   TEXT                -- shown in "on sale soon"; never again
);
CREATE INDEX IF NOT EXISTS idx_listings_date ON listings(local_date);

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
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.executescript(SCHEMA)
    if version < 3:
        _migrate_to_listings(conn)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


def _migrate_to_listings(conn: sqlite3.Connection) -> None:
    """v2 kept Ticketmaster events in ``events_seen`` and their showings in
    ``surfaced``. Carried into ``listings`` so nothing already sent is sent
    again: every event the old digest was handed counts as considered. Not as
    shown: v2 counted every event it handed the model as surfaced, mentioned
    or not, so carrying that over made each one a week-of reminder."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "events_seen" in tables:
        conn.execute(
            """INSERT OR IGNORE INTO listings (id, name, acts, local_date, venue, presales, first_seen, considered_at)
               SELECT event_id, COALESCE(name, ''), '[]', substr(starts_at, 1, 10), venue, '[]', first_seen, first_seen
               FROM events_seen"""
        )
        conn.execute("DROP TABLE events_seen")
    for row in conn.execute("SELECT * FROM surfaced WHERE kind IN ('event', 'presale')").fetchall():
        tm_id = row["item_id"].rsplit("/", 1)[-1]
        conn.execute(
            """INSERT OR IGNORE INTO listings (id, name, acts, presales, first_seen, considered_at)
               VALUES (?, ?, '[]', '[]', ?, ?)""",
            (tm_id, row["summary"] or "", row["first_seen"], row["first_seen"]),
        )
        if row["kind"] == "presale":
            conn.execute("UPDATE listings SET onsale_shown_at = ? WHERE id = ?", (row["last_seen"], tm_id))
    conn.execute("DELETE FROM surfaced WHERE kind IN ('event', 'presale')")


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


LISTING_FIELDS = ("name", "acts", "local_date", "local_time", "venue", "city", "distance_miles",
                  "url", "onsale_at", "presales")


def upsert_listing(conn: sqlite3.Connection, listing: dict) -> None:
    """Store what Ticketmaster says now. ``first_seen`` and the shown/considered
    columns are ours and survive; everything else is refreshed. ``acts`` and
    ``presales`` are stored as JSON."""
    values = [json.dumps(listing.get(f) or []) if f in ("acts", "presales") else listing.get(f)
              for f in LISTING_FIELDS]
    columns = ", ".join(LISTING_FIELDS)
    # A listing found by the on-sale query carries no distance band; keep the one we had.
    updates = ", ".join(
        f"{f} = COALESCE(excluded.{f}, listings.{f})" if f == "distance_miles" else f"{f} = excluded.{f}"
        for f in LISTING_FIELDS
    )
    conn.execute(
        f"INSERT INTO listings (id, {columns}, first_seen) VALUES (?, {', '.join('?' * len(LISTING_FIELDS))}, ?) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        (listing["id"], *values, utcnow()),
    )


def listings_by_id(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    rows = conn.execute(f"SELECT * FROM listings WHERE id IN ({', '.join('?' * len(ids))})", ids).fetchall()
    return {row["id"]: row for row in rows}


def mark_listings(conn: sqlite3.Connection, ids: list[str], what: str) -> None:
    """``considered`` (offered to the model), ``event`` (shown in the events
    section) or ``onsale`` (shown in on-sale soon)."""
    if not ids:
        return
    sets = {
        "considered": "considered_at = COALESCE(considered_at, :now)",
        "event": "event_shown_at = :now, event_times_shown = event_times_shown + 1",
        "onsale": "onsale_shown_at = :now",
    }[what]
    marks = ", ".join(f":id{i}" for i in range(len(ids)))
    conn.execute(f"UPDATE listings SET {sets} WHERE id IN ({marks})",
                 {"now": utcnow(), **{f"id{i}": v for i, v in enumerate(ids)}})


# How long rows are kept. Nothing here is precious (see the module docstring);
# this only stops the file growing forever.
LISTING_DAYS_AFTER = 30      # after the show's date
PRICE_HISTORY_DAYS = 365
DECIDED_WRITES_DAYS = 90
SURFACED_DAYS = 180          # since last raised; one raised again just counts afresh


def prune(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    """Delete what has outlived its use. Returns rows removed per table."""
    now = now or datetime.now(timezone.utc)

    def ago(days: int) -> str:
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    removed = {
        "listings": conn.execute(
            "DELETE FROM listings WHERE local_date < ? OR (local_date IS NULL AND first_seen < ?)",
            ((now - timedelta(days=LISTING_DAYS_AFTER)).date().isoformat(), ago(90)),
        ).rowcount,
        "price_history": conn.execute(
            "DELETE FROM price_history WHERE checked_at < ?", (ago(PRICE_HISTORY_DAYS),)).rowcount,
        "pending_writes": conn.execute(
            "DELETE FROM pending_writes WHERE status != 'pending' AND decided_at < ?",
            (ago(DECIDED_WRITES_DAYS),)).rowcount,
        "surfaced": conn.execute("DELETE FROM surfaced WHERE last_seen < ?", (ago(SURFACED_DAYS),)).rowcount,
    }
    return {table: n for table, n in removed.items() if n}


def shown_count(conn: sqlite3.Connection, item_id: str) -> int:
    """How many times an item has been shown, without recording a new one.

    Used by a dry run: it needs the same "mentioned before" context a real
    run would use for tone, but must not itself count as a showing.
    """
    row = conn.execute("SELECT times_shown FROM surfaced WHERE item_id = ?", (item_id,)).fetchone()
    return int(row["times_shown"]) if row else 0


def last_surfaced(conn: sqlite3.Connection, item_id: str) -> str | None:
    row = conn.execute("SELECT last_seen FROM surfaced WHERE item_id = ?", (item_id,)).fetchone()
    return row["last_seen"] if row else None


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
