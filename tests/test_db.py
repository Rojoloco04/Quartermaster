"""state.db: price history, listings, the v2 migration and pruning.

Nothing here is the only copy of anything - db.py's own rule - so these tests
care about the two properties that rule depends on: a failed price check must
never look like "no change", and first_seen must never move once set.
"""

from pathlib import Path

from quartermaster import db


def test_latest_price_ignores_failed_checks(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.record_price_check(conn, "https://x/1", "Widget", 1000, "USD", ok=True)
        db.record_price_check(conn, "https://x/1", "Widget", None, "USD", ok=False, note="http 503")

        latest = db.latest_price(conn, "https://x/1")
        assert latest["price_cents"] == 1000, "a failed check must not be mistaken for a price"


def test_latest_price_is_the_most_recent_successful_check(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.record_price_check(conn, "https://x/1", "Widget", 2000, "USD", ok=True)
        db.record_price_check(conn, "https://x/1", "Widget", 1500, "USD", ok=True)

        assert db.latest_price(conn, "https://x/1")["price_cents"] == 1500


def test_no_successful_check_means_no_latest_price(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.record_price_check(conn, "https://x/1", "Widget", None, "USD", ok=False, note="couldn't check")
        assert db.latest_price(conn, "https://x/1") is None


def test_shown_count_starts_at_zero_and_does_not_mutate(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        assert db.shown_count(conn, "event:id/e1") == 0
        assert db.shown_count(conn, "event:id/e1") == 0, "reading shown_count must never itself count as a showing"


def test_record_surfaced_increments_and_shown_count_agrees(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        assert db.record_surfaced(conn, "event:id/e1", "event", "Show") == 1
        assert db.record_surfaced(conn, "event:id/e1", "event", "Show") == 2
        assert db.shown_count(conn, "event:id/e1") == 2


def listing(id_: str, **kw) -> dict:
    return {"id": id_, "name": "Show", "acts": ["Tool"], "local_date": "2026-10-10", "local_time": "20:00",
            "venue": "The Pageant", "city": "St. Louis", "distance_miles": 3.0, "url": "u", "onsale_at": "",
            "presales": [], **kw}


def test_a_listing_refreshes_but_keeps_first_seen_and_what_we_showed(tmp_path: Path):
    with db.session(tmp_path / "state.db") as conn:
        db.upsert_listing(conn, listing("e1"))
        first = db.listings_by_id(conn, ["e1"])["e1"]["first_seen"]
        db.mark_listings(conn, ["e1"], "event")
        db.upsert_listing(conn, listing("e1", name="Show (moved)", distance_miles=None))
        row = db.listings_by_id(conn, ["e1"])["e1"]
        assert row["first_seen"] == first and row["name"] == "Show (moved)"
        assert row["event_times_shown"] == 1 and row["event_shown_at"]
        assert row["distance_miles"] == 3.0, "an on-sale query's listing has no distance; keep the known one"
        assert row["acts"] == '["Tool"]'


def test_v2_history_carries_over_so_nothing_is_sent_twice(tmp_path: Path):
    import sqlite3

    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE surfaced (item_id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, times_shown INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE events_seen (event_id TEXT PRIMARY KEY, name TEXT, starts_at TEXT, band TEXT,
            venue TEXT, first_seen TEXT NOT NULL);
        INSERT INTO events_seen VALUES ('e1', 'Show', '2026-10-10T01:00:00Z', 'local', 'Pageant', '2026-09-22T00:00:00+00:00');
        INSERT INTO surfaced VALUES ('event:artist/Tool/e1', 'event', 'Show', 't0', 't1', 2);
        INSERT INTO surfaced VALUES ('presale:artist/LCS/p9', 'presale', 'LCS', 't0', 't3', 4);
        INSERT INTO surfaced VALUES ('stale:abc', 'stale', 'Page', 't0', 't1', 1);
        PRAGMA user_version = 2;
    """)
    old.close()
    with db.session(path) as conn:
        rows = db.listings_by_id(conn, ["e1", "p9"])
        # v2 marked everything handed to the model as surfaced: considered, not shown.
        assert rows["e1"]["considered_at"] and not rows["e1"]["event_shown_at"]
        assert rows["p9"]["onsale_shown_at"] == "t3"
        assert [r["kind"] for r in conn.execute("SELECT kind FROM surfaced")] == ["stale"]
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'events_seen'").fetchone()


def test_a_v3_database_gains_the_v4_columns(tmp_path: Path):
    import sqlite3

    path = tmp_path / "state.db"
    db.connect(path).close()
    old = sqlite3.connect(path)
    old.executescript("""
        ALTER TABLE listings DROP COLUMN segment; ALTER TABLE listings DROP COLUMN genres;
        ALTER TABLE listings DROP COLUMN considered_taste; PRAGMA user_version = 3;""")
    old.close()
    with db.session(path) as conn:
        db.upsert_listing(conn, listing("e1", genres=["Hip-Hop/Rap"], segment="Music"))
        db.mark_listings(conn, ["e1"], "considered", taste="abc")
        row = db.listings_by_id(conn, ["e1"])["e1"]
        assert (row["genres"], row["segment"], row["considered_taste"]) == ('["Hip-Hop/Rap"]', "Music", "abc")


def test_an_up_to_date_database_opens_while_another_connection_is_writing(tmp_path: Path):
    path = tmp_path / "state.db"
    writer = db.connect(path)
    db.upsert_listing(writer, listing("e1"))  # an open write transaction, as during a digest
    try:
        reader = db.connect(path)  # used to write user_version here and wait out the lock
        assert db.listings_by_id(reader, ["e1"]) == {}
        reader.close()
    finally:
        writer.close()


def test_prune_drops_what_has_outlived_its_use(tmp_path: Path):
    from datetime import datetime, timezone

    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    with db.session(tmp_path / "state.db") as conn:
        db.upsert_listing(conn, listing("past", local_date="2026-10-01"))
        db.upsert_listing(conn, listing("soon", local_date="2026-11-20"))
        conn.execute("INSERT INTO price_history (url, ok, checked_at) VALUES ('u', 1, '2025-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO price_history (url, ok, checked_at) VALUES ('u', 1, '2026-11-30T00:00:00+00:00')")
        removed = db.prune(conn, now)
        assert removed == {"listings": 1, "price_history": 1}
        assert list(db.listings_by_id(conn, ["past", "soon"])) == ["soon"]
